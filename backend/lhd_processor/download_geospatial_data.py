import os
import requests
import shutil
import tempfile
import zipfile
import rasterio
import pandas as pd
import pynhd as nhd
from pynhd import NLDI
import geopandas as gpd
from pyproj import Transformer
from shapely.ops import transform
from shapely.geometry import box
from datetime import datetime, timedelta
import time
import traceback
from typing import Optional, Union, Tuple, List

# Direct NLDI endpoint for the initial COMID lookup. We bypass pynhd's
# comid_byloc here because its pygeoutils JSON parser crashes (surfaces as
# "CRS attribute" AttributeError) on certain server responses, which would
# make ~70% of NID-sourced dam coordinates fail before the navigation step.
_NLDI_BASE = "https://labs-beta.waterdata.usgs.gov/api/nldi/linked-data"
_NLDI_COMID_URL = f"{_NLDI_BASE}/comid/position"


def _fetch_seed_flowline(comid: int, timeout: float = 30) -> Optional[gpd.GeoDataFrame]:
    """Return a 1-row GeoDataFrame with the geometry of `comid`, or None.

    Used as a fallback when neither upstream nor downstream navigation
    returns anything (e.g. headwater + terminal reach, coastline,
    disconnected canal). Lets us at least produce a flowline gpkg
    containing the seed reach so the dam isn't fully dropped from the
    pipeline.
    """
    try:
        r = requests.get(f"{_NLDI_BASE}/comid/{int(comid)}", timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    feats = payload.get("features") or []
    if not feats:
        return None
    try:
        gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    except Exception:
        return None
    if gdf.empty:
        return None
    if "comid" not in gdf.columns and "identifier" in gdf.columns:
        gdf = gdf.rename(columns={"identifier": "comid"})
    return gdf


def _lookup_comid_direct(lon: float, lat: float, timeout: float = 30) -> Optional[int]:
    """Return the NHDPlus V2 COMID nearest (lon, lat), or None if NLDI 404s.

    No retries (caller decides). 404 means NLDI has no flowline at that point.
    """
    try:
        r = requests.get(_NLDI_COMID_URL, params={"coords": f"POINT({lon} {lat})"}, timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    feats = payload.get("features") or []
    if not feats:
        return None
    props = feats[0].get("properties") or {}
    val = props.get("comid") or props.get("identifier")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

from rasterio.merge import merge as rio_merge
from rasterio.warp import Resampling, reproject


def sanitize_filename(filename):
    """
        some files have '/' in their name like "1/3 arc-second," so we'll fix it
    """
    invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    for char in invalid_chars:
        filename = filename.replace(char, "-")  # Replace invalid characters with '_'
    return filename


# =================================================================
# 1. FLOWLINE FUNCTIONS (NHDPlus)
# =================================================================

def download_nhd_flowline(lat: float, lon: float, flowline_dir: str, distance_km: Union[float, Tuple[float, float], List[float]] = (1, 2), site_id=None, vaa_df=None):
    """
    Uses HyRiver NLDI to fetch NHDPlus flowlines, merges VAAs (hydroseq, dnhydroseq),
    and standardizes ID columns.

    vaa_df : pre-fetched result of nhd.nhdplus_vaa(). When provided, the function
             skips the download entirely — pass this when calling from a parallel
             context to avoid concurrent writes to the shared cache file.
    """
    try:
        if distance_km is None:
            distance_km = (1, 2)

        # Retry NLDI initialization as it fetches metadata from the web and can fail
        nldi = None
        for attempt in range(3):
            try:
                nldi = NLDI()
                # Explicitly check if nldi was actually created
                if nldi is None:
                    raise ValueError("NLDI service returned None")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)  # Increase sleep time
                else:
                    print(f"Failed to initialize NLDI after retries: {e}")
                    return None, None

        # Direct HTTP call to NLDI for COMID resolution — see _lookup_comid_direct
        # for why we bypass pynhd here.
        comid_val = _lookup_comid_direct(lon, lat)
        if comid_val is None:
            return None, None
        
        # Create site-specific subdirectory if site_id is provided
        if site_id:
            site_flowline_dir = os.path.join(flowline_dir, str(site_id))
        else:
            site_flowline_dir = flowline_dir
            
        os.makedirs(site_flowline_dir, exist_ok=True)
        output_path = os.path.join(site_flowline_dir, f"nhd_flowline_{comid_val}.gpkg")
        
        if os.path.exists(output_path):
            try:
                # print(f"Found existing flowline file: {output_path}") # User asked to limit prints
                gdf = gpd.read_file(output_path)
                return output_path, gdf
            except Exception as e:
                print(f"Error reading existing file, redownloading: {e}")

        all_reaches = []
        # upstreamTributaries walks every branch (catches braided / canal /
        # side-channel networks that main-stem navigation skips); downstreamMain
        # stays on the main path because tributaries downstream rarely make
        # sense from a screening perspective.
        nav_modes = [("upstreamTributaries", "up"), ("downstreamMain", "down")]

        for mode, direction in nav_modes:
            try:
                # Handle distance logic safely
                dist = distance_km
                if isinstance(distance_km, (tuple, list)):
                    if len(distance_km) == 2:
                        dist = distance_km[0] if direction == "up" else distance_km[1]
                    elif len(distance_km) > 0:
                        dist = distance_km[0]
                    else:
                        dist = 1 # Default fallback

                reach = nldi.navigate_byid(
                    fsource="comid",
                    fid=str(comid_val),
                    navigation=mode,
                    source="flowlines",
                    distance=dist
                )
                if reach is not None and not reach.empty:
                    all_reaches.append(reach)
            except Exception as e:
                print(f"NLDI Navigation Error ({mode}): {e}")

        if not all_reaches:
            # Fallback: both nav directions came back empty (headwater +
            # terminal reach, coastline, disconnected canal). Save just the
            # seed COMID's own geometry so the dam still gets a flowline file.
            seed = _fetch_seed_flowline(comid_val)
            if seed is None or seed.empty:
                print(f"  Both navigations empty AND seed fetch failed for COMID {comid_val} @ ({lat:.4f},{lon:.4f})")
                return None, None
            print(f"  Both navigations empty — using seed-only flowline for COMID {comid_val}")
            all_reaches.append(seed)

        combined_df = pd.concat(all_reaches)
        combined_df.columns = combined_df.columns.str.lower()

        # Standardization: Ensure 'nhdplusid' exists
        id_map = ['nhdplus_comid', 'comid', 'nhdplusid']
        for col in id_map:
            if col in combined_df.columns:
                combined_df = combined_df.rename(columns={col: 'nhdplusid'})
                break

        # --- FETCH AND MERGE VAAs ---
        # Convert ID to numeric for the join
        if 'nhdplusid' in combined_df.columns:
            combined_df['nhdplusid'] = pd.to_numeric(combined_df['nhdplusid'])

            try:
                if vaa_df is None:
                    vaa_df = nhd.nhdplus_vaa()
                if vaa_df is not None and not vaa_df.empty:
                    vaa_subset = vaa_df[['comid', 'hydroseq', 'dnhydroseq']].rename(columns={'comid': 'nhdplusid'})

                    # Merge VAAs into the flowline dataframe
                    combined_df = combined_df.merge(vaa_subset, on='nhdplusid', how='left')
            except Exception as e:
                print(f"Warning: Could not fetch/merge VAAs: {e}")
        # ----------------------------

        combined_gdf = gpd.GeoDataFrame(combined_df, crs=all_reaches[0].crs)
        if 'nhdplusid' in combined_gdf.columns:
            combined_gdf = combined_gdf.drop_duplicates(subset='nhdplusid')

        # output_path is already defined above
        if not os.path.exists(output_path):
            combined_gdf.to_file(output_path, driver="GPKG", layer="NHDFlowline")
            print(f"Flowlines saved to: {output_path}")

        return output_path, combined_gdf

    except Exception as e:
        print(f"Unexpected error in download_nhd_flowline: {e}")
        traceback.print_exc()
        return None, None


# =================================================================
# 2. DEM FUNCTIONS (3DEP & METADATA)
# =================================================================

def query_dem_tiles(lhd_id, flowline_gdf, buffer_deg: float = 0.002) -> List[dict]:
    """
    Queries the USGS TNM API for DEM tiles covering the flowline bounding box.
    Tries 1-m tiles first, then 1/9 arc-second, then 1/3 arc-second.
    Returns a list of dicts: {url, filename, dataset, resolution_m}.
    """
    if flowline_gdf is None or flowline_gdf.empty:
        return []

    if flowline_gdf.crs and flowline_gdf.crs.to_epsg() != 4326:
        flowline_gdf = flowline_gdf.to_crs(epsg=4326)

    b = flowline_gdf.total_bounds
    bbox = (
        float(b[0] - buffer_deg), float(b[1] - buffer_deg),
        float(b[2] + buffer_deg), float(b[3] + buffer_deg),
    )

    tnm_url = "https://tnmaccess.nationalmap.gov/api/v1/products"
    priority = [
        ("Digital Elevation Model (DEM) 1 meter", 1),
        ("National Elevation Dataset (NED) 1/9 arc-second", 3),
        ("National Elevation Dataset (NED) 1/3 arc-second Current", 10),
    ]

    for dataset_name, res_m in priority:
        params = {
            "datasets": dataset_name,
            "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
            "outputFormat": "JSON",
        }
        try:
            r = requests.get(tnm_url, params=params, timeout=30)
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as e:
            print(f"Dam {lhd_id}: TNM query failed for '{dataset_name}': {e}")
            continue

        tiles = [
            {
                "url": item["downloadURL"],
                "filename": sanitize_filename(os.path.basename(item["downloadURL"])),
                "dataset": dataset_name,
                "resolution_m": res_m,
            }
            for item in items
            if item.get("downloadURL")
        ]
        if tiles:
            return tiles

    print(f"Dam {lhd_id}: No DEM tiles found in TNM API.")
    return []


_RASTER_EXTS = (".tif", ".tiff", ".img")


def _extract_zipped_raster(zip_path: str, raw_dem_dir: str) -> Union[str, None]:
    """
    Extract the first raster (.tif/.tiff/.img) member from `zip_path` into
    `raw_dem_dir` (flattening any internal directory structure), delete the
    zip, and return the extracted file path. Returns None on failure.

    USGS TNM serves 1/9 arc-second NED tiles as .zip archives wrapping an
    .img raster; the rest of the pipeline expects a bare raster on disk.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raster_members = [
                m for m in zf.namelist()
                if not m.endswith("/")
                and os.path.basename(m).lower().endswith(_RASTER_EXTS)
            ]
            if not raster_members:
                print(f"Error: no raster (.tif/.img) inside {zip_path}")
                return None
            member = raster_members[0]
            out_name = sanitize_filename(os.path.basename(member))
            out_path = os.path.join(raw_dem_dir, out_name)
            with zf.open(member) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.remove(zip_path)
        return out_path
    except Exception as e:
        print(f"Error extracting {zip_path}: {e}")
        return None


def download_raw_tile(tile_url: str, raw_dem_dir: str) -> Union[str, None]:
    """
    Downloads a single DEM tile to raw_dem_dir if not already present.
    If the downloaded payload is a .zip (e.g. 1/9 arc-second NED tiles),
    extracts the inner raster and removes the zip.
    Returns the local raster file path on success, None on failure.
    """
    filename = sanitize_filename(os.path.basename(tile_url))
    local_path = os.path.join(raw_dem_dir, filename)

    # Cache hit: a prior run may have left either the raster or an unextracted zip.
    if os.path.exists(local_path):
        if local_path.lower().endswith(".zip"):
            extracted = _extract_zipped_raster(local_path, raw_dem_dir)
            return extracted  # may be None if the zip is corrupt
        return local_path

    os.makedirs(raw_dem_dir, exist_ok=True)
    try:
        r = requests.get(tile_url, stream=True, timeout=120)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    except Exception as e:
        print(f"Error downloading {tile_url}: {e}")
        if os.path.exists(local_path):
            os.remove(local_path)
        return None

    if local_path.lower().endswith(".zip"):
        return _extract_zipped_raster(local_path, raw_dem_dir)
    return local_path


# =================================================================
# 3. BASEFLOW & LIDAR DATE ESTIMATION
# =================================================================

def find_water_gpstime(lat, lon):
    """
    Queries USGS TNM for raw LiDAR points to find the collection date.
    """
    bbox = (lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001)
    params = {"bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
              "datasets": "Lidar Point Cloud (LPC)", "max": 1, "outputFormat": "JSON"}

    try:
        r = requests.get("https://tnmaccess.nationalmap.gov/api/v1/products", params=params)
        items = r.json().get("items", [])
        if not items: return None

        dl_url = items[0].get("downloadURL")
        with tempfile.TemporaryDirectory() as tmp:
            las_data = requests.get(dl_url).content
            las_path = os.path.join(tmp, "data.las")
            with open(las_path, 'wb') as f: f.write(las_data)

            import laspy
            las = laspy.read(las_path)
            # Standard conversion from GPS seconds to Date
            gps_time = las.gps_time[0] + 1_000_000_000
            return gps_time
    except Exception:
        return None


def est_dem_baseflow(stream_reach, source, method, project_hint=None):
    lat, lon = stream_reach.latitude, stream_reach.longitude
    found_date = None

    if "lidar date" in method.lower():
        # Step 1: Try the high-precision LiDAR file method
        lidar_time = find_water_gpstime(lat, lon)

        if lidar_time:
            found_date = (datetime(1980, 1, 6) + timedelta(seconds=lidar_time)).strftime('%Y-%m-%d')

        # Step 2: Fallback to the project_hint if the .las download failed
        elif project_hint:
            # Example: extract '2019' from 'Iowa_Lidar_2019'
            import re
            match = re.search(r'20\d{2}', project_hint)
            if match:
                found_date = f"{match.group(0)}-06-15"  # Assume mid-summer for the year
                print(f"Using estimated date from project name: {found_date}")

        # Step 3: Get the flow
        if found_date:
            flow = stream_reach.get_flow_on_date(found_date, source)
        else:
            flow = stream_reach.get_median_flow(source)

        return flow, found_date


# =================================================================
# 4. LAND USE FUNCTIONS (ESA WorldCover Alignment)
# =================================================================

def download_land_raster(lhd_id: int, dem_path: str, land_dir: str):
    """
    Downloads ESA WorldCover and aligns it exactly to the grid of the downloaded DEM.
    """
    # Create site-specific subdirectory for the final raster
    site_land_dir = os.path.join(land_dir, str(lhd_id))
    os.makedirs(site_land_dir, exist_ok=True)
    
    final_path = os.path.join(site_land_dir, f"{lhd_id}_LAND_Raster.tif")
    if os.path.exists(final_path): return final_path

    # Get target DEM specs
    with rasterio.open(dem_path) as src:
        ncols, nrows = src.width, src.height
        dem_transform = src.transform
        dem_crs = src.crs
        # Create polygon for ESA intersection
        geom = box(*src.bounds)

    # Keep raw ESA tiles in the main LAND folder to share across sites
    raw_esa_dir = os.path.join(land_dir, 'raw_esa')
    raw_lc_tif = _download_esa_land_cover(geom, raw_esa_dir, dem_crs)

    if raw_lc_tif:
        # Reproject onto the DEM's grid so the pixels line up exactly
        with rasterio.open(raw_lc_tif) as src:
            profile = src.profile
            profile.update(driver='GTiff', crs=dem_crs, transform=dem_transform,
                           width=ncols, height=nrows, compress='lzw')
            for key in ('blockxsize', 'blockysize', 'tiled'):
                profile.pop(key, None)
            with rasterio.open(final_path, 'w', **profile) as dst:
                for band in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, band),
                        destination=rasterio.band(dst, band),
                        dst_transform=dem_transform,
                        dst_crs=dem_crs,
                        resampling=Resampling.nearest,
                    )
        return final_path
    return None


def _download_esa_land_cover(geom, output_dir, dem_crs, year=2021):
    # Reproject geometry to WGS84 for ESA S3 query
    transformer = Transformer.from_crs(dem_crs, "EPSG:4326", always_xy=True)
    geom_wgs84 = transform(transformer.transform, geom)

    grid = gpd.read_file("https://esa-worldcover.s3.eu-central-1.amazonaws.com/esa_worldcover_grid.geojson")
    tiles = grid[grid.intersects(geom_wgs84)]

    os.makedirs(output_dir, exist_ok=True)
    downloaded = []
    for tile in tiles.ll_tile:
        url = f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/{year}/map/ESA_WorldCover_10m_{year}_v200_{tile}_Map.tif"
        path = os.path.join(output_dir, os.path.basename(url))
        
        # Validate existing file
        if os.path.exists(path):
            try:
                with rasterio.open(path) as src:
                    pass
            except Exception:
                print(f"Removing corrupted file: {path}")
                os.remove(path)

        if not os.path.exists(path):
            print(f"Downloading {url}...")
            try:
                resp = requests.get(url, stream=True)
                if resp.status_code == 200:
                    with open(path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            f.write(chunk)
                else:
                    print(f"Failed to download {url}: Status {resp.status_code}")
            except Exception as e:
                print(f"Download error for {url}: {e}")

        if os.path.exists(path):
            downloaded.append(path)

    if not downloaded:
        return None

    merged_path = os.path.join(output_dir, "merged_esa.tif")
    try:
        # Only read the site's window (plus a small pad) — full ESA tiles are ~1 GB each
        pad = 0.01
        minx, miny, maxx, maxy = geom_wgs84.bounds
        data, transform = rio_merge(downloaded, bounds=(minx - pad, miny - pad, maxx + pad, maxy + pad))
        with rasterio.open(downloaded[0]) as first:
            profile = first.profile
        profile.update(driver='GTiff', height=data.shape[1], width=data.shape[2],
                       count=data.shape[0], transform=transform, compress='lzw')
        for key in ('blockxsize', 'blockysize', 'tiled'):
            profile.pop(key, None)
        with rasterio.open(merged_path, 'w', **profile) as dst:
            dst.write(data)
        return merged_path
    except Exception as e:
        print(f"Error merging ESA tiles: {e}")
        return None
