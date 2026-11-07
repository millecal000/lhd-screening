"""
Build trimmed per-dam DEMs and matching constant land cover rasters from
the tiles staged by stage_nhd_dem.py.

For each dam in tile_manifest.json:
  1. Load the staged flowline .gpkg from STRM/{dam_id}/.
  2. Reproject the flowline to its UTM zone, buffer total_bounds by ARC's
     perpendicular X-section reach (default 500 m), then convert that bbox
     back to EPSG:4326.
  3. Merge the dam's staged tiles into a rectangular DEM trimmed to that
     bbox  →  DEM/{dam_id}/dem_{dam_id}.tif.
  4. Write a constant-value land cover raster on the same grid plus a
     Manning_n.txt mapping the single LC code to n=0.035  →
     LAND/{dam_id}/constant_land.tif and Manning_n.txt.

Existing outputs are reused; pass --force to rebuild.

Usage
-----
    python backend/build_trimmed_dems.py --staging-dir /data/lhd_staging
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import geopandas as gpd
from pyproj import Transformer

_FAILURES_FILENAME = "dam_failures.json"


def _load_failures(staging_dir: Path) -> Dict[int, dict]:
    p = staging_dir / _FAILURES_FILENAME
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_failures(staging_dir: Path, failures: Dict[int, dict]) -> None:
    p = staging_dir / _FAILURES_FILENAME
    out = {str(k): v for k, v in failures.items()}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(p)

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge as rio_merge
from rasterio.warp import transform_bounds

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.land_raster import make_constant_land_raster

# --- Optional GDAL Python bindings --------------------------------------------
# GDAL's own Python bindings ("osgeo") come with the conda environment
# (environment.yml) but are not installed by `uv sync`. When they are present,
# _mosaic_and_clip uses gdal.Warp, which is what built every DEM before the
# 2026-10-27 switch to uv and is known to work: it reprojects tiles from
# different UTM zones onto one grid and honours each tile's nodata value.
# When they are absent it falls back to rasterio.merge, which has two known
# problems (see _mosaic_and_clip_rasterio). Run DEM building in conda.
try:
    from osgeo import gdal as _gdal
    _gdal.UseExceptions()  # raise Python errors instead of returning None
except ImportError:  # uv environment: no GDAL bindings
    _gdal = None

_X_SECTION_DIST_M = 500.0

# --- Guard against oversized flowline files ---------------------------------
# Each dam's DEM is clipped to the bounding box of its NHD flowline file
# (STRM/<dam_id>/nhd_flowline_<COMID>.gpkg) plus a buffer. Normally that file
# holds ~1 km of river upstream and ~1.5 km downstream of the dam, so the DEM
# covers a few km^2.
#
# Occasionally NLDI's "upstreamTributaries" navigation returns a whole river
# network instead: hundreds of segments spread over tens of km. Example: dams
# 9276, 9544, 9655 and 10460 in huc6_160503 had flowline files covering
# ~72 x 78 km. Clipping a 1 m DEM to that area means billions of pixels
# (~22 GB of memory) and a file that hit the classic-TIFF 4 GB limit and was
# truncated (unreadable).
#
# The WSP height calculation (run_wsp_pipeline._process_dam) only samples the
# DEM along the dam's own reach and the reach directly upstream, so a DEM over
# the whole network is never needed. When a flowline file is unusually large,
# _flowline_bbox_wgs84 therefore sizes the DEM from the segments near the dam's
# own reach instead (see _segments_near_seed_reach).

# A flowline file whose bounding box is wider OR taller than this (in metres,
# measured in the local UTM zone) is treated as oversized. 20 km is well above
# any normal file (the largest normal one seen spans ~6 x 16 km), so ordinary
# dams are never affected and their DEMs stay identical to earlier runs.
_MAX_FLOWLINE_EXTENT_M = 20_000.0

# For an oversized file, keep only segments that come within this distance (m)
# of the dam's own reach. 3 km comfortably covers the dam reach, the reach
# upstream of it, and the 500 m downstream search window used by the WSP step.
_NEAR_SEED_REACH_M = 3_000.0


def _pick_dem_compression() -> str:
    """Use ZSTD when this rasterio build supports it, otherwise DEFLATE.

    Both are lossless. ZSTD is faster and slightly smaller; DEFLATE is readable
    by every GDAL build, so it is the safe fallback.
    """
    try:
        with MemoryFile() as mem:
            with mem.open(driver="GTiff", width=16, height=16, count=1,
                          dtype="float32", compress="zstd",
                          transform=rasterio.Affine(1, 0, 0, 0, -1, 16)) as test:
                test.write(np.zeros((1, 16, 16), dtype="float32"))
        return "zstd"
    except Exception:
        return "deflate"


_DEM_COMPRESSION = _pick_dem_compression()


def _dem_write_options(dtype: str) -> dict:
    """GeoTIFF creation options for trimmed DEMs.

    - lossless compression with a predictor (3 for floating-point elevations,
      2 for integers), which shrinks DEMs to roughly 40% of plain LZW
    - 512x512 internal tiles instead of one-row strips
    - BIGTIFF=IF_SAFER so a large clip switches to BigTIFF instead of being
      truncated at the 4 GB classic-TIFF limit
    """
    return {
        "compress": _DEM_COMPRESSION,
        "predictor": 3 if np.issubdtype(np.dtype(dtype), np.floating) else 2,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "BIGTIFF": "IF_SAFER",
    }


def _mosaic_and_clip(tile_paths: List[str], bbox_wgs84, out_path: Path) -> None:
    """Mosaic DEM tiles and clip them to a WGS84 bbox, writing a compressed GeoTIFF.

    Uses gdal.Warp when GDAL's Python bindings are installed (conda), otherwise
    rasterio.merge (uv). Both write with _dem_write_options (lossless
    compression, predictor, 512x512 tiles, BigTIFF safety). The method used is
    logged once per run.
    """
    if _gdal is not None:
        _log_mosaic_method_once("gdal.Warp (GDAL Python bindings)")
        _mosaic_and_clip_gdal(tile_paths, bbox_wgs84, out_path)
    else:
        _log_mosaic_method_once(
            "rasterio.merge -- WARNING: GDAL Python bindings not found. This "
            "method fails on tiles from different UTM zones (CRS mismatch) and "
            "writes all-zero DEMs from 1/9 arc-second .img tiles. Build DEMs "
            "in the conda environment (environment.yml) instead."
        )
        _mosaic_and_clip_rasterio(tile_paths, bbox_wgs84, out_path)


_mosaic_method_logged = False


def _log_mosaic_method_once(method: str) -> None:
    """Print which mosaic method is in use, the first time a DEM is built."""
    global _mosaic_method_logged
    if not _mosaic_method_logged:
        _mosaic_method_logged = True
        _log(f"DEM mosaic method: {method}")


def _gdal_creation_options(dtype: str) -> List[str]:
    """_dem_write_options translated into gdal.Warp creation options.

    Same settings as the rasterio path, so DEMs are identical in format either
    way: lossless ZSTD (DEFLATE if this GDAL build lacks ZSTD), predictor 3
    for floating-point data, 512x512 internal tiles, BIGTIFF=IF_SAFER.
    """
    opts = _dem_write_options(dtype)
    # The ZSTD check in _pick_dem_compression used rasterio's GDAL; check this
    # GDAL build too, since conda's GDAL can differ from rasterio's.
    gtiff_options = _gdal.GetDriverByName("GTiff").GetMetadataItem("DMD_CREATIONOPTIONLIST") or ""
    compress = "ZSTD" if "ZSTD" in gtiff_options else "DEFLATE"
    return [
        f"COMPRESS={compress}",
        f"PREDICTOR={opts['predictor']}",
        "TILED=YES",
        f"BLOCKXSIZE={opts['blockxsize']}",
        f"BLOCKYSIZE={opts['blockysize']}",
        f"BIGTIFF={opts['BIGTIFF']}",
    ]


def _mosaic_and_clip_gdal(tile_paths: List[str], bbox_wgs84, out_path: Path) -> None:
    """gdal.Warp mosaic + clip: the pre-uv method, now with compression.

    - Output grid: the first tile's CRS and resolution (gdal.Warp's default),
      so DEMs match those built before 2026-10-27.
    - Tiles in other CRSs (e.g. neighbouring UTM zones) are reprojected onto
      that grid instead of being rejected.
    - Each tile's nodata value is honoured, including the -3.4e38 used by the
      1/9 arc-second .img tiles.
    - outputBounds/outputBoundsSRS clip to the dam's WGS84 bbox.
    """
    # Read the data type from the first tile so the right predictor is chosen
    # (3 for floating-point elevations, 2 for integers).
    first = _gdal.Open(str(tile_paths[0]))
    dtype = _gdal.GetDataTypeName(first.GetRasterBand(1).DataType).lower()  # e.g. "float32"
    first = None  # close the file

    _gdal.Warp(
        str(out_path),
        [str(p) for p in tile_paths],
        format="GTiff",
        outputBounds=bbox_wgs84,          # (lon_min, lat_min, lon_max, lat_max)
        outputBoundsSRS="EPSG:4326",      # the bbox is in WGS84 degrees
        resampleAlg="bilinear",           # same as before 2026-10-27
        creationOptions=_gdal_creation_options(dtype),
    )


def _mosaic_and_clip_rasterio(tile_paths: List[str], bbox_wgs84, out_path: Path) -> None:
    """rasterio.merge mosaic + clip (used only when GDAL bindings are missing).

    Known problems, found in the 2026-11-07 rebuild test of huc6_030300:
    - Tiles in different CRSs (e.g. UTM zones 17 and 18) raise
      "CRS mismatch with source", so the dam gets no DEM.
    - 1/9 arc-second .img tiles (nodata -3.4e38) come out as all zeros, so the
      WSP step returns negative_delta_wse for those dams.
    """
    with rasterio.open(tile_paths[0]) as first:
        src_crs = first.crs
        profile = first.profile
    bounds = transform_bounds("EPSG:4326", src_crs, *bbox_wgs84, densify_pts=21)
    data, transform = rio_merge(tile_paths, bounds=bounds, resampling=Resampling.bilinear)
    # Drop the source tiles' storage settings; the clip gets its own below.
    for key in ("blockxsize", "blockysize", "tiled", "compress", "predictor", "interleave"):
        profile.pop(key, None)
    profile.update(
        driver="GTiff",
        height=data.shape[1],
        width=data.shape[2],
        count=data.shape[0],
        transform=transform,
        dtype=data.dtype.name,
        **_dem_write_options(data.dtype.name),
    )
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(data)

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _flowline_bbox_wgs84(gpkg_path: Path, buffer_m: float) -> Tuple[float, float, float, float]:
    """Return the area to clip this dam's DEM to, as a WGS84 bbox.

    The box is the flowline's extent plus ``buffer_m`` on every side, computed
    in the local UTM zone (so the buffer is in real metres) and converted back
    to longitude/latitude for clipping.

    Oversized flowline files (wider or taller than _MAX_FLOWLINE_EXTENT_M) are
    trimmed first to the segments near the dam's own reach; see the comment
    block above _MAX_FLOWLINE_EXTENT_M for why. Normal files are unchanged.

    Returns (lon_min, lat_min, lon_max, lat_max).
    """
    # Read the flowline file and make sure it is in WGS84 (EPSG:4326) lon/lat.
    gdf = gpd.read_file(gpkg_path)
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    # Project to the UTM zone at the flowline's centre so distances (the size
    # check and the buffer) are measured in metres rather than degrees.
    centroid = gdf.geometry.union_all().centroid
    utm_epsg = _utm_epsg(centroid.x, centroid.y)
    gdf_utm = gdf.to_crs(epsg=utm_epsg)
    minx, miny, maxx, maxy = gdf_utm.total_bounds

    # Oversized flowline file? Shrink the box to the segments near the dam's
    # reach so the DEM stays a sensible size. Logged so it shows up in the run.
    if max(maxx - minx, maxy - miny) > _MAX_FLOWLINE_EXTENT_M:
        trimmed = _segments_near_seed_reach(gdf_utm, gpkg_path)
        if trimmed is not None:
            _log(
                f"  {gpkg_path.name}: flowline spans {(maxx - minx) / 1000:.1f} x "
                f"{(maxy - miny) / 1000:.1f} km ({len(gdf_utm)} segments); using "
                f"{len(trimmed)} segment(s) within {_NEAR_SEED_REACH_M / 1000:g} km "
                f"of the dam reach"
            )
            minx, miny, maxx, maxy = trimmed.total_bounds
        else:
            # The dam's reach could not be identified in the file, so there is
            # nothing safe to trim around. Keep the full extent (old behaviour)
            # but warn, because this DEM may be very large.
            _log(
                f"  WARN {gpkg_path.name}: flowline spans {(maxx - minx) / 1000:.1f} x "
                f"{(maxy - miny) / 1000:.1f} km but the dam reach was not found in it; "
                f"using the full extent"
            )

    # Add the buffer (metres) on every side of the box.
    minx -= buffer_m
    miny -= buffer_m
    maxx += buffer_m
    maxy += buffer_m

    # Convert the corners back to longitude/latitude for the clipping step.
    transformer = Transformer.from_crs(utm_epsg, 4326, always_xy=True)
    lon_min, lat_min = transformer.transform(minx, miny)
    lon_max, lat_max = transformer.transform(maxx, maxy)
    return lon_min, lat_min, lon_max, lat_max


def _segments_near_seed_reach(gdf_utm: gpd.GeoDataFrame, gpkg_path: Path):
    """Return the flowline segments within _NEAR_SEED_REACH_M of the dam's reach.

    The dam's own reach ("seed" reach) is the segment whose ID matches the
    COMID in the file name: stage_nhd_dem.py saves each dam's flowline as
    ``nhd_flowline_<COMID>.gpkg``, where COMID is the reach the dam sits on.

    ``gdf_utm`` must already be in a projected (metre-based) CRS so the
    distance below is in metres.

    Returns a GeoDataFrame of the nearby segments (always including the seed
    reach itself), or None if the seed reach can't be identified, in which case
    the caller keeps the full extent and logs a warning.
    """
    # 1. Read the dam's COMID from the file name, e.g. nhd_flowline_10737989.gpkg.
    m = re.search(r"nhd_flowline_(\d+)", gpkg_path.stem)
    if not m:
        return None
    seed_comid = int(m.group(1))

    # 2. Find the column that holds each segment's ID. NHD sources name it
    #    differently, so match case-insensitively against the usual names.
    cols = {c.lower(): c for c in gdf_utm.columns}
    id_col = next((cols[c] for c in ("nhdplusid", "comid", "nhdplus_comid") if c in cols), None)
    if id_col is None:
        return None

    # 3. Pick out the seed reach. IDs may be stored as text, floats or blanks,
    #    so convert them to numbers; anything unreadable becomes NaN and simply
    #    doesn't match.
    import pandas as pd  # already loaded by geopandas; imported here for clarity
    ids = pd.to_numeric(gdf_utm[id_col], errors="coerce")
    seed = gdf_utm[ids == seed_comid]
    if seed.empty:
        return None

    # 4. Keep every segment that touches a zone extending _NEAR_SEED_REACH_M
    #    around the seed reach. This includes the reach upstream of the dam,
    #    which the WSP step samples, and drops the distant parts of the network.
    zone = seed.geometry.union_all().buffer(_NEAR_SEED_REACH_M)
    near = gdf_utm[gdf_utm.geometry.intersects(zone)]

    # The seed reach always intersects its own zone, so ``near`` is never empty
    # in practice; fall back to the seed alone just in case.
    return near if not near.empty else seed


def _build_one(
    idx: int,
    total: int,
    dam_id: int,
    flowline_dir: Path,
    raw_dem_dir: Path,
    dem_out_dir: Path,
    land_out_dir: Path,
    tile_filenames: List[str],
    buffer_m: float,
    force: bool,
    skip_land: bool = False,
) -> Tuple[int, str]:
    site_dem_dir = dem_out_dir / str(dam_id)
    site_land_dir = land_out_dir / str(dam_id)
    dem_path = site_dem_dir / f"dem_{dam_id}.tif"
    land_path = site_land_dir / "constant_land.tif"
    manning_path = site_land_dir / "Manning_n.txt"

    dem_ready = dem_path.exists()
    land_ready = skip_land or (land_path.exists() and manning_path.exists())
    if not force and dem_ready and land_ready:
        _log(f"[{idx}/{total}] Dam {dam_id}: cached")
        return dam_id, "cached"

    site_strm_dir = flowline_dir / str(dam_id)
    gpkg_files = list(site_strm_dir.glob("nhd_flowline_*.gpkg"))
    if not gpkg_files:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no flowline gpkg)")
        return dam_id, "no-flowline"
    gpkg_path = gpkg_files[0]

    if not tile_filenames:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no tiles in manifest)")
        return dam_id, "no-tiles"

    tile_paths = [str(raw_dem_dir / fn) for fn in tile_filenames if (raw_dem_dir / fn).exists()]
    if not tile_paths:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no staged tiles on disk)")
        return dam_id, "tiles-missing"
    if len(tile_paths) != len(tile_filenames):
        missing = len(tile_filenames) - len(tile_paths)
        _log(
            f"[{idx}/{total}] Dam {dam_id}: WARN — {missing} of "
            f"{len(tile_filenames)} manifest tiles not on disk; using {len(tile_paths)}"
        )

    try:
        bbox = _flowline_bbox_wgs84(gpkg_path, buffer_m)
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL bbox ({e})")
        return dam_id, f"bbox-error:{e}"

    site_dem_dir.mkdir(parents=True, exist_ok=True)
    try:
        _mosaic_and_clip(tile_paths, bbox, dem_path)
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL warp ({e})")
        return dam_id, f"warp-error:{e}"

    if not dem_path.exists():
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL (warp produced no file)")
        return dam_id, "warp-empty"

    if not skip_land:
        try:
            make_constant_land_raster(str(dem_path), site_land_dir)
        except Exception as e:
            _log(f"[{idx}/{total}] Dam {dam_id}: FAIL land ({e})")
            return dam_id, f"land-error:{e}"

    _log(f"[{idx}/{total}] Dam {dam_id}: ok ({len(tile_paths)} tiles → {dem_path.name})")
    return dam_id, "ok"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-dir", required=True, type=Path,
                        help="Root of the staging tree produced by stage_nhd_dem.py")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N dams from the manifest")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel workers [default: 8]")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild trimmed DEM + land cover even if cached on disk")
    parser.add_argument("--no-land", action="store_true",
                        help="Skip writing constant_land.tif and Manning_n.txt (WSP pipeline)")
    parser.add_argument("--buffer-m", type=float, default=_X_SECTION_DIST_M,
                        help=f"Metric buffer around flowline bbox [default: {_X_SECTION_DIST_M:g} m]")
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    flowline_dir = staging_dir / "STRM"
    raw_dem_dir = staging_dir / "DEM" / "raw_3dep"
    dem_out_dir = staging_dir / "DEM"
    land_out_dir = staging_dir / "LAND"
    if not args.no_land:
        land_out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = staging_dir / "tile_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Manifest not found: {manifest_path}\nRun stage_nhd_dem.py first.")
    with open(manifest_path) as f:
        manifest = json.load(f)
    dam_tiles: Dict[int, List[str]] = {
        int(k): v for k, v in manifest.get("dam_tiles", {}).items()
    }

    items = list(dam_tiles.items())
    if args.limit:
        items = items[: args.limit]

    # Skip dams already on record as failed in a prior run. rolling_pipeline's
    # --retry-failed flag wipes this file before invoking us; standalone
    # callers can delete dam_failures.json by hand.
    failures = _load_failures(staging_dir)
    if failures:
        before = len(items)
        items = [(did, fns) for did, fns in items if did not in failures]
        n_skipped = before - len(items)
        if n_skipped:
            print(
                f"Skipping {n_skipped} dam(s) marked failed in "
                f"{_FAILURES_FILENAME}. Delete the file or rerun "
                f"rolling_pipeline with --retry-failed to retry them.\n"
            )

    total = len(items)
    land_note = " (no-land)" if args.no_land else " + constant land cover"
    print(
        f"Building trimmed DEMs{land_note} for {total} dams "
        f"(workers={args.workers}, buffer={args.buffer_m:g} m)\n"
    )

    results: Dict[int, str] = {}
    counts: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                _build_one,
                i + 1, total, dam_id,
                flowline_dir, raw_dem_dir, dem_out_dir, land_out_dir,
                tile_filenames, args.buffer_m, args.force, args.no_land,
            ): dam_id
            for i, (dam_id, tile_filenames) in enumerate(items)
        }
        for fut in as_completed(futures):
            dam_id = futures[fut]
            try:
                _, status = fut.result()
            except Exception as e:
                status = f"worker-error:{e}"
                _log(f"  ! Dam {dam_id} worker error: {e}")
            results[dam_id] = status
            counts[status] = counts.get(status, 0) + 1

    # Persist this run's per-dam outcomes:
    #   - ok / cached     → drop from failures (success this round)
    #   - anything else   → add/refresh failure record
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_new_failures = 0
    for dam_id, status in results.items():
        if status in ("ok", "cached"):
            failures.pop(dam_id, None)
        else:
            failures[dam_id] = {
                "stage": "build_trimmed_dems",
                "cause": status,
                "at": now,
            }
            n_new_failures += 1
    _save_failures(staging_dir, failures)

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:<20} {counts[status]}")
    print(f"\nFailure registry: {len(failures)} dam(s) on record "
          f"(+{n_new_failures} this run) → {_FAILURES_FILENAME}")


if __name__ == "__main__":
    main()
