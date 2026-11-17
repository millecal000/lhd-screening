"""
estimate_dam_height_lite.py — standalone dam-height estimator for validation
against the ARC pipeline (Quintana & Hotchkiss 2026).

For each dam (OBJECTID-keyed) the script:
  1. Resolves upstream/downstream ComIDs from the NHDPlus VAA table.
  2. Fetches a 3DEP 1 m or 3 m DEM tile (reuses ARC staging dir if present).
  3. Derives a synthetic rating curve (SRC) for the downstream ComID using
     bankfull trapezoidal channel geometry from the lynker-spatial hydrofabric
     parquet (already cached at backend/cache/ — same source as the SRC viz
     feature).  Route_Link.nc from s3://noaa-nwm-pds/ was the original target
     but that bucket only carries rolling forecast output, not domain parameter
     files; the hydrofabric parquet is an equivalent, better-formatted source.
  4. Finds the nearest USGS streamgage (≤50 km, drainage-area ratio 0.5–1.5),
     retrieves the daily flow on the 3DEP LiDAR collection date, converts it
     to a flow-duration-curve percentile from the gage's full record, then
     applies that percentile to the NWM v3 retrospective FDC at the target
     ComID to derive Q_lidar.  Q50 is the 50th-percentile NWM retrospective
     flow at the downstream ComID.
  5. Extracts a longitudinal DEM profile along the NHD flowline using the
     Method 2 flat-zone crest picker (same logic as screening/reach.py, but
     operating on the raw DEM rather than ARC Curve.csv).
  6. Computes:
       downstream_bed  = downstream_WSE − SRC.stage(Q_lidar)
       H_overflow      = (Q50 / (Cd · L)) ^ (2/3)   [broad-crested, Cd=1.70 SI]
       crest_elev      = pool_WSE − H_overflow
       dam_height      = crest_elev − downstream_bed

Usage
-----
    python backend/estimate_dam_height_lite.py \\
        --staging-dir /data/lhd_staging \\
        --limit 100 \\
        --output-csv output/dam_height_lite.csv

    # Process specific OBJECTIDs:
    python backend/estimate_dam_height_lite.py \\
        --objectids 2330 5017 8842 \\
        --staging-dir /data/lhd_staging \\
        --output-csv output/dam_height_lite.csv

Constraints
-----------
- Does NOT run ARC or modify any ARC pipeline files.
- Does NOT produce results for dams where the best available DEM is coarser
  than 3 m — a "dem_too_coarse" flag is written and the row skipped.
- Writes a `flags` column enumerating every fallback or failure mode so
  downstream validation can filter on quality.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table
import geopandas as gpd
import rasterio
import rasterio.merge
import requests
from pyproj import Transformer
from shapely.geometry import Point
from shapely.ops import linemerge, unary_union

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.download_geospatial_data import (
    query_dem_tiles,
    download_raw_tile,
    _lookup_comid_direct,
    _fetch_seed_flowline,
)

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_TABLE   = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
DEFAULT_OUT     = _REPO_ROOT / "output" / "dam_height_lite.csv"
DEFAULT_CACHE   = _BACKEND_ROOT / "cache"
HYDROFABRIC_PKL = DEFAULT_CACHE / "riverml_channel_geometry_with_ahg.parquet"
NWM_FDC_CACHE   = DEFAULT_CACHE / "nwm_fdc_api_v2.parquet"
NWM_API_BASE    = "https://nwm-api-v2-9f6idmxh.uc.gateway.dev"

# Broad-crested weir coefficient (SI), Cd ≈ 1.70
Cd = 1.70

MAX_DEM_RES_M = 3.0   # skip dam if best available tile is coarser than this

# Percentiles available from the NWM API v2 /percentile-flows endpoint
_NWM_QUANTILE_LEVELS = [0.00, 0.02, 0.05, 0.10, 0.20, 0.25, 0.30, 0.50, 0.75, 0.90, 0.95, 0.99, 1.00]
_NWM_Q_COLS = [f"q{int(q*100):03d}" for q in _NWM_QUANTILE_LEVELS]
# → q000, q002, q005, q010, q020, q025, q030, q050, q075, q090, q095, q099, q100

OUTPUT_COLS = [
    "OBJECTID", "comid_snap", "comid_up", "comid_dn",
    "dem_resolution_m", "lidar_date",
    "Q_lidar_cms", "Q50_cms",
    "L_m", "L_method",
    "pool_WSE_m", "downstream_WSE_m", "downstream_bed_m",
    "H_overflow_m", "crest_elev_m", "dam_height_m",
    "method", "flags",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[lite] {msg}", flush=True)


# ---------------------------------------------------------------------------
# NWM API key
# ---------------------------------------------------------------------------

def _nwm_api_key() -> str:
    key = os.environ.get("NWM_API_KEY", "")
    if key:
        return key
    env_path = _REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("NWM_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    return ""


# ---------------------------------------------------------------------------
# ComID helpers
# ---------------------------------------------------------------------------

def _coerce_comid(val) -> Optional[int]:
    try:
        v = float(val)
        if np.isfinite(v) and v > 0:
            return int(v)
    except (TypeError, ValueError):
        pass
    return None


def _find_adjacent_comids(snap_comid: int, vaa_df: pd.DataFrame):
    """Return (comid_up, comid_dn) using VAA hydroseq linkages.

    Uses uphydroseq / dnhydroseq — no NLDI call needed.
    Returns None for either direction if not found in VAA.
    """
    row = vaa_df[vaa_df["comid"] == snap_comid]
    if row.empty:
        return None, None

    up_hydroseq = int(row.iloc[0]["uphydroseq"])
    dn_hydroseq = int(row.iloc[0]["dnhydroseq"])

    comid_up = None
    if up_hydroseq > 0:
        up_row = vaa_df[vaa_df["hydroseq"] == up_hydroseq]
        if not up_row.empty:
            comid_up = int(up_row.iloc[0]["comid"])

    comid_dn = None
    if dn_hydroseq > 0:
        dn_row = vaa_df[vaa_df["hydroseq"] == dn_hydroseq]
        if not dn_row.empty:
            comid_dn = int(dn_row.iloc[0]["comid"])

    return comid_up, comid_dn


# ---------------------------------------------------------------------------
# Flowline
# ---------------------------------------------------------------------------

def _load_or_fetch_flowline(
    dam_id: int, snap_comid: int, lat: float, lon: float,
    staging_dir: Optional[Path],
) -> Optional[gpd.GeoDataFrame]:
    """Return a GeoDataFrame for the snap reach.  Prefers cached gpkg."""
    if staging_dir is not None:
        site_dir = staging_dir / "STRM" / str(dam_id)
        gpkgs = list(site_dir.glob("nhd_flowline_*.gpkg")) if site_dir.exists() else []
        if gpkgs:
            try:
                gdf = gpd.read_file(gpkgs[0])
                gdf.columns = gdf.columns.str.lower()
                # Normalize ComID column name
                for col in ("nhdplusid", "nhdplus_comid", "comid"):
                    if col in gdf.columns:
                        gdf = gdf.rename(columns={col: "nhdplusid"})
                        break
                gdf["nhdplusid"] = pd.to_numeric(gdf["nhdplusid"], errors="coerce")
                snap_rows = gdf[gdf["nhdplusid"] == snap_comid]
                if not snap_rows.empty:
                    return snap_rows
                return gdf  # return full gpkg if snap not found by id
            except Exception as e:
                _log(f"  warning: cached gpkg read failed ({e}) — fetching fresh")

    # Fall back to fetching the seed geometry only (lightweight — no navigation)
    gdf = _fetch_seed_flowline(snap_comid)
    if gdf is not None and not gdf.empty:
        gdf.columns = gdf.columns.str.lower()
        for col in ("comid", "identifier", "nhdplus_comid"):
            if col in gdf.columns:
                gdf = gdf.rename(columns={col: "nhdplusid"})
                break
        return gdf
    return None


# ---------------------------------------------------------------------------
# DEM
# ---------------------------------------------------------------------------

def _resolve_dem(
    dam_id: int, lat: float, lon: float,
    flowline_gdf: gpd.GeoDataFrame,
    staging_dir: Optional[Path],
    cache_dir: Path,
) -> tuple[Optional[list[str]], Optional[float]]:
    """Return ([raster_paths], resolution_m) or (None, None).

    Reuses ARC-staged DEM tiles when staging_dir is given.
    Enforces MAX_DEM_RES_M — returns (None, None) if only coarser data exist.
    """
    # Check existing staged tiles
    if staging_dir is not None:
        dem_site = staging_dir / "DEM" / str(dam_id)
        existing = list(dem_site.glob(f"dem_{dam_id}.tif"))
        if existing:
            try:
                with rasterio.open(existing[0]) as src:
                    res_m = abs(src.transform.a)
                    if src.crs and src.crs.is_projected:
                        res_m = abs(src.transform.a)
                    else:
                        # geographic CRS: approx metres
                        res_m = abs(src.transform.a) * 111320 * np.cos(np.radians(lat))
                if res_m <= MAX_DEM_RES_M:
                    return [str(existing[0])], res_m
            except Exception:
                pass

        # Check raw tile cache
        raw_dir = staging_dir / "DEM" / "raw_3dep"
        if raw_dir.exists():
            manifest_path = staging_dir / "tile_manifest.json"
            if manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)
                tile_catalog = manifest.get("tile_catalog", {})
                dam_tiles = manifest.get("dam_tiles", {}).get(str(dam_id), [])
                paths = []
                res_m = None
                for fn in dam_tiles:
                    meta = tile_catalog.get(fn, {})
                    p = raw_dir / fn
                    if p.exists():
                        r = float(meta.get("resolution_m", 999))
                        if r <= MAX_DEM_RES_M:
                            paths.append(str(p))
                            if res_m is None or r < res_m:
                                res_m = r
                if paths:
                    return paths, res_m

    # Fresh query
    tiles = query_dem_tiles(dam_id, flowline_gdf)
    if not tiles:
        return None, None

    best_res = tiles[0]["resolution_m"]
    if best_res > MAX_DEM_RES_M:
        return None, best_res  # signal: coarser than allowed

    raw_dir = cache_dir / "raw_3dep"
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for tile in tiles:
        if tile["resolution_m"] != best_res:
            break
        p = download_raw_tile(tile["url"], str(raw_dir))
        if p:
            paths.append(p)
    return (paths if paths else None), best_res


# ---------------------------------------------------------------------------
# LiDAR date
# ---------------------------------------------------------------------------

def _get_lidar_date(lat: float, lon: float) -> Optional[str]:
    """Return the LiDAR survey date (YYYY-MM-DD) from TNM metadata.

    Queries the LPC product endpoint first; falls back to the 1-m DEM
    product's publicationDate.  No .las file is downloaded.
    """
    eps = 0.001
    bbox = f"{lon-eps},{lat-eps},{lon+eps},{lat+eps}"
    base = "https://tnmaccess.nationalmap.gov/api/v1/products"

    for dataset in (
        "Lidar Point Cloud (LPC)",
        "Digital Elevation Model (DEM) 1 meter",
    ):
        try:
            r = requests.get(
                base,
                params={"datasets": dataset, "bbox": bbox, "max": 1, "outputFormat": "JSON"},
                timeout=20,
            )
            items = r.json().get("items", [])
            if items:
                for field in ("publicationDate", "dateCreated", "lastUpdated"):
                    val = items[0].get(field, "")
                    if val and len(val) >= 10:
                        return str(val)[:10]
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Channel geometry / SRC from hydrofabric parquet
# ---------------------------------------------------------------------------

def _load_channel_geometry(comids: set[int]) -> pd.DataFrame:
    """Filter hydrofabric geometry parquet to the needed ComIDs."""
    import pyarrow.parquet as pq

    if not HYDROFABRIC_PKL.exists():
        raise FileNotFoundError(
            f"Hydrofabric parquet not found at {HYDROFABRIC_PKL}.\n"
            "Run: python backend/build_synthetic_rating_curves.py"
        )
    tbl = pq.read_table(
        HYDROFABRIC_PKL,
        columns=["feature_id", "owp_y_bf", "owp_tw_bf", "bf_area",
                 "owp_roughness_bathy", "owp_roughness_no_bathy", "slope",
                 "y_coef", "y_exp"],
        filters=[("feature_id", "in", comids)],
    )
    df = tbl.to_pandas().set_index("feature_id")
    return df


_MIN_SLOPE = 1e-4
_MIN_BW_FRAC = 0.05
_N_SRC_PTS = 60
_AHG_Y_EXP_MIN, _AHG_Y_EXP_MAX = 0.1, 0.9


def _build_src(geom_row: pd.Series, q_max_cms: Optional[float] = None):
    """Return a (stage→Q) callable built from AHG when available, Manning's trapezoid otherwise.

    AHG (primary): depth = y_coef * Q^y_exp — the same relationship NWM uses
    internally for routing, calibrated across the full flow range including
    out-of-bank flows.  Q extends to q_max_cms (NWM p100) so the curve covers
    100% of the FDC.

    Trapezoid (fallback): Manning's equation on equivalent trapezoidal cross-section,
    in-channel only, capped at 2.5× bankfull stage.
    """
    y_coef = float(geom_row.get("y_coef", np.nan))
    y_exp  = float(geom_row.get("y_exp",  np.nan))
    y_bf   = float(geom_row.get("owp_y_bf", np.nan))

    use_ahg = (
        np.isfinite(y_coef) and y_coef > 0 and
        np.isfinite(y_exp) and _AHG_Y_EXP_MIN <= y_exp <= _AHG_Y_EXP_MAX
    )

    if use_ahg:
        Q_bf = ((y_bf / y_coef) ** (1.0 / y_exp)
                if np.isfinite(y_bf) and y_bf > 0 else 10.0)
        q_upper = (q_max_cms * 1.1 if q_max_cms else Q_bf * 20.0)
        q_upper = max(q_upper, 1.0)

        Q      = np.logspace(np.log10(max(Q_bf * 1e-4, 1e-3)), np.log10(q_upper), _N_SRC_PTS)
        stages = y_coef * Q ** y_exp

        def src(stage_m: float) -> Optional[float]:
            if stage_m <= 0:
                return 0.0
            return float(np.interp(stage_m, stages, Q))

        src.stages = stages
        src.Q      = Q
        src.y_bf   = float(y_coef * Q_bf ** y_exp) if np.isfinite(Q_bf) else y_bf
        src.method = "ahg"
        return src

    # --- Manning's trapezoid fallback ---
    tw_bf = float(geom_row.get("owp_tw_bf", np.nan))
    area  = float(geom_row.get("bf_area",   np.nan))
    slope = float(geom_row.get("slope",     np.nan))
    n     = geom_row.get("owp_roughness_bathy")
    if pd.isna(n) or float(n) <= 0:
        n = geom_row.get("owp_roughness_no_bathy")

    if not all(np.isfinite(v) and v > 0 for v in (y_bf, tw_bf, area, float(n) if n is not None else 0)):
        return None

    slope = max(float(slope) if np.isfinite(slope) else _MIN_SLOPE, _MIN_SLOPE)
    n = float(n)
    z  = max((tw_bf - area / y_bf) / y_bf, 0.0)
    bw = max(tw_bf - 2 * z * y_bf, _MIN_BW_FRAC * tw_bf)

    stages = np.linspace(1e-3, y_bf * 2.5, _N_SRC_PTS)
    a_arr  = stages * bw + z * stages ** 2
    perim  = bw + 2 * stages * np.sqrt(1 + z ** 2)
    R      = a_arr / perim
    Q      = (1.0 / n) * a_arr * R ** (2.0 / 3.0) * slope ** 0.5

    def src(stage_m: float) -> Optional[float]:
        if stage_m <= 0:
            return 0.0
        if stage_m > stages[-1]:
            return None
        return float(np.interp(stage_m, stages, Q))

    src.stages = stages
    src.Q      = Q
    src.y_bf   = y_bf
    src.method = "trapezoid"
    return src


# ---------------------------------------------------------------------------
# NWM API — FDC via /percentile-flows
# ---------------------------------------------------------------------------

def _api_field_for_quantile(q: float) -> str:
    """Map a quantile level (0–1) to the NWM API v2 field name.

    API schema: p0 → "min", p2–p99 → "perc_N" (no zero-padding), p100 → "max"
    """
    p = int(round(q * 100))
    if p == 0:
        return "min"
    if p == 100:
        return "max"
    return f"perc_{p}"


def _parse_fdc_record(record: dict) -> Optional[dict[str, float]]:
    """Extract {q_col: cms} from a single /percentile-flows API record."""
    reach_id = record.get("reach_id")
    if reach_id is None:
        return None
    try:
        reach_id = int(reach_id)
    except (TypeError, ValueError):
        return None

    row: dict[str, float] = {"feature_id": reach_id}
    for q, col in zip(_NWM_QUANTILE_LEVELS, _NWM_Q_COLS):
        raw = record.get(_api_field_for_quantile(q))
        row[col] = float(raw) if raw is not None else np.nan

    if np.isnan(row.get("q050", np.nan)):
        return None
    return row


def batch_fetch_nwm_fdc(needed_comids: list[int]) -> dict[int, dict]:
    """Return {comid: {q_col: discharge_cms}} for all needed_comids.

    Loads from cache first; fetches missing ComIDs from NWM API v2
    /percentile-flows in a single batched request, then appends to cache.
    """
    result: dict[int, dict] = {}

    if NWM_FDC_CACHE.exists():
        cached = pd.read_parquet(NWM_FDC_CACHE)
        q_cols_present = [c for c in _NWM_Q_COLS if c in cached.columns]
        for _, row in cached[cached["feature_id"].isin(needed_comids)].iterrows():
            fid = int(row["feature_id"])
            result[fid] = {col: float(row[col]) for col in q_cols_present}
        _log(f"NWM FDC cache: {len(result)}/{len(needed_comids)} hits")

    missing = [c for c in needed_comids if c not in result]
    if not missing:
        return result

    key = _nwm_api_key()
    if not key:
        _log("WARNING: NWM_API_KEY not set — set it in .env or environment")
        return result

    pct_str   = ",".join(str(int(q * 100)) for q in _NWM_QUANTILE_LEVELS)
    reach_str = ",".join(str(c) for c in missing)
    _log(f"Fetching NWM FDC for {len(missing)} ComID(s) via /percentile-flows …")

    try:
        resp = requests.get(
            f"{NWM_API_BASE}/percentile-flows",
            params={"reach_id": reach_str, "percentiles": pct_str,
                    "output_format": "json", "key": key},
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        _log(f"  /percentile-flows error: {e}")
        return result

    # Unwrap GeoJSON FeatureCollection or plain list
    if isinstance(payload, dict) and "features" in payload:
        records = [f.get("properties", f) for f in payload["features"]]
    elif isinstance(payload, list):
        records = payload
    else:
        _log(f"  unexpected response type: {type(payload)}")
        return result

    new_rows = []
    for record in records:
        parsed = _parse_fdc_record(record)
        if parsed is None:
            continue
        fid = int(parsed["feature_id"])
        new_rows.append(parsed)
        result[fid] = {col: parsed[col] for col in _NWM_Q_COLS}

    _log(f"  parsed {len(new_rows)}/{len(records)} records from API")

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if NWM_FDC_CACHE.exists():
            old_df = pd.read_parquet(NWM_FDC_CACHE)
            combined = pd.concat([old_df, new_df], ignore_index=True).drop_duplicates(
                subset="feature_id", keep="last"
            )
        else:
            combined = new_df
        NWM_FDC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(NWM_FDC_CACHE, index=False)
        _log(f"  cached FDC for {len(new_rows)} ComID(s) → {NWM_FDC_CACHE}")

    return result


def _nwm_quantile(fdc_row: dict, percentile: float) -> Optional[float]:
    """Interpolate discharge at `percentile` (0–1) from a cached FDC row."""
    vals = [fdc_row.get(col) for col in _NWM_Q_COLS]
    if any(v is None or not np.isfinite(v) for v in vals):
        return None
    return float(np.interp(percentile, _NWM_QUANTILE_LEVELS, vals))


# ---------------------------------------------------------------------------
# NWM API — Q_lidar via /retrospectives
# ---------------------------------------------------------------------------

_RETRO_START = "1979-02-01"
_RETRO_END   = "2023-02-01"


def _fetch_q_lidar_api(comid: int, date_str: str) -> Optional[float]:
    """Return NWM streamflow (cms) at `comid` on `date_str` (YYYY-MM-DD).

    Queries /retrospectives for the 24-hour window on the LiDAR collection
    date and returns the daily mean. Replaces the USGS gage → FDC percentile
    chain — gives the actual NWM flow at the reach on the day the LiDAR was
    collected, with no drainage-area correction needed.

    Returns None if the date is outside the NWM v3 retrospective window
    (1979-02-01 – 2023-02-01), letting the caller fall back to Q50.
    """
    if not (_RETRO_START <= date_str <= _RETRO_END):
        return None

    key = _nwm_api_key()
    if not key:
        return None

    start = f"{date_str}T00:00:00"
    end   = f"{date_str}T23:59:59"

    try:
        resp = requests.get(
            f"{NWM_API_BASE}/retrospectives",
            params={"reach_id": str(comid), "start_time": start,
                    "end_time": end, "output_format": "json", "key": key},
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        _log(f"  /retrospectives error (comid={comid}, date={date_str}): {e}")
        return None

    # Unwrap envelope
    if isinstance(payload, dict) and "features" in payload:
        records = [f.get("properties", f) for f in payload["features"]]
    elif isinstance(payload, list):
        records = payload
    else:
        return None

    flows = []
    for rec in records:
        v = rec.get("streamflow")
        if v is not None:
            try:
                flows.append(float(v))
            except (TypeError, ValueError):
                pass

    if not flows:
        return None
    return float(np.nanmean(flows))


# ---------------------------------------------------------------------------
# Longitudinal DEM profile
# ---------------------------------------------------------------------------

def _extract_profile(
    dem_paths: list[str],
    reach_gdf: gpd.GeoDataFrame,
    dam_lat: float,
    dam_lon: float,
    snap_comid: int,
    sample_interval_m: float = 2.0,
) -> Optional[pd.DataFrame]:
    """Sample DEM elevations along the NHD reach geometry.

    Returns a DataFrame with columns ``x_m`` (signed chainage from dam snap,
    negative = upstream) and ``DEM_Elev`` (metres), or None on failure.
    """
    # Open / mosaic DEM
    try:
        if len(dem_paths) == 1:
            src_ctx = rasterio.open(dem_paths[0])
        else:
            from rasterio.merge import merge as rio_merge
            srcs = [rasterio.open(p) for p in dem_paths]
            mosaic, transform = rio_merge(srcs)
            # Write temp mosaic
            import tempfile, os
            tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
            tmp.close()
            with rasterio.open(
                tmp.name, "w",
                driver="GTiff",
                height=mosaic.shape[1],
                width=mosaic.shape[2],
                count=1,
                dtype=mosaic.dtype,
                crs=srcs[0].crs,
                transform=transform,
            ) as dst:
                dst.write(mosaic[0], 1)
            for s in srcs:
                s.close()
            src_ctx = rasterio.open(tmp.name)
    except Exception as e:
        _log(f"    profile: DEM open failed: {e}")
        return None

    with src_ctx as dem:
        dem_crs = dem.crs
        nodata  = dem.nodata
        res_m   = abs(dem.transform.a)
        interval = max(sample_interval_m, res_m)

        # Filter reach GDF to snap ComID
        for col in ("nhdplusid", "comid", "identifier"):
            if col in reach_gdf.columns:
                snap_rows = reach_gdf[
                    pd.to_numeric(reach_gdf[col], errors="coerce") == snap_comid
                ]
                if not snap_rows.empty:
                    reach_gdf = snap_rows
                    break

        # Build a single LineString for the reach
        geoms = reach_gdf.geometry
        if geoms.crs is None or geoms.crs.to_epsg() == 4326:
            # Reproject to DEM CRS for metric sampling
            geoms = geoms.to_crs(dem_crs)
        elif geoms.crs != dem_crs:
            geoms = geoms.to_crs(dem_crs)

        union = unary_union(geoms)
        if union.is_empty:
            return None
        if union.geom_type == "MultiLineString":
            merged = linemerge(union)
            if merged.geom_type == "MultiLineString":
                merged = max(merged.geoms, key=lambda g: g.length)
        elif union.geom_type == "LineString":
            merged = union
        else:
            return None

        total_len = merged.length
        if total_len < 10:
            return None

        # Project dam point onto line
        tf = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
        dam_x, dam_y = tf.transform(dam_lon, dam_lat)
        dam_pt  = Point(dam_x, dam_y)
        d_snap  = merged.project(dam_pt)  # distance from upstream end

        # Generate sample points at `interval` spacing
        distances = np.arange(0, total_len + interval, interval)
        points    = [merged.interpolate(d) for d in distances]

        # Sample DEM
        coords = [(p.x, p.y) for p in points]
        elev_vals = list(dem.sample(coords))
        elevs = np.array([e[0] if (nodata is None or e[0] != nodata) else np.nan
                          for e in elev_vals])

        x_m = distances - d_snap  # signed: negative upstream, positive downstream

        profile = pd.DataFrame({"x_m": x_m, "DEM_Elev": elevs})
        profile = profile.dropna(subset=["DEM_Elev"])
        if len(profile) < 4:
            return None

        # Sanity check orientation: upstream (x<0) should be higher than downstream (x>0)
        up  = profile[profile["x_m"] < -5]["DEM_Elev"].mean()
        dn  = profile[profile["x_m"] >  5]["DEM_Elev"].mean()
        if np.isfinite(up) and np.isfinite(dn) and up < dn:
            profile["x_m"] = -profile["x_m"]

        return profile.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Crest and downstream pickers (Method 2 / Method B — no ARC dependency)
# ---------------------------------------------------------------------------

def _pick_crest(
    profile: pd.DataFrame,
    search_m: float = 50.0,
    flat_threshold: float = 0.02,
) -> Optional[pd.Series]:
    """Method 2 flat-zone crest picker on a raw DEM profile.

    Mirrors screening/reach.py:pick_crest_cell but uses the raster profile
    DataFrame (x_m, DEM_Elev) instead of ARC Curve.csv.
    """
    upstream = profile[(profile["x_m"] < 0) & (profile["x_m"] >= -search_m)].copy()
    if upstream.empty:
        return profile.iloc[(profile["x_m"].abs()).argsort()].iloc[0]

    x = profile["x_m"].values
    y = profile["DEM_Elev"].values
    grad = np.gradient(y, x)
    upstream = upstream.assign(grad_abs=np.abs(grad[upstream.index]))
    upstream = upstream.sort_values("x_m", ascending=False)  # closest first

    for _, row in upstream.iterrows():
        if row["grad_abs"] < flat_threshold:
            return row
    return upstream.loc[upstream["grad_abs"].idxmin()]


def _pick_downstream(
    profile: pd.DataFrame,
    ref_min: float = 100.0,
    ref_max: float = 500.0,
) -> Optional[pd.Series]:
    """Method B slope-reconvergence downstream picker on a raw DEM profile.

    Mirrors screening/reach.py:pick_downstream_cell.
    """
    downstream = profile[profile["x_m"] > 0].copy()
    if downstream.empty:
        return None

    x = profile["x_m"].values
    y = profile["DEM_Elev"].values
    grad = np.abs(np.gradient(y, x))  # magnitude of slope (m/m)

    profile = profile.assign(slope_abs=grad)
    downstream = profile[profile["x_m"] > 0].copy()

    ref_band = downstream[(downstream["x_m"] >= ref_min) & (downstream["x_m"] <= ref_max)]
    ref_slope = float(ref_band["slope_abs"].median()) if not ref_band.empty else np.nan

    if not np.isfinite(ref_slope) or ref_slope <= 0:
        return downstream.sort_values("x_m").iloc[0]

    downstream["slope_diff"] = (downstream["slope_abs"] - ref_slope).abs() / ref_slope
    return downstream.loc[downstream["slope_diff"].idxmin()]


# ---------------------------------------------------------------------------
# Width estimation
# ---------------------------------------------------------------------------

def _estimate_width(
    dam_row: pd.Series,
    geom_row: Optional[pd.Series],
) -> tuple[Optional[float], str]:
    """Return (L_m, method_str) for the dam crest width.

    Priority:
    1. Weir_Length from CSV (field measurement, in metres).
    2. Spillway_Width_Ft from NID (converted to metres).
    3. Hydrofabric bankfull top width owp_tw_bf (channel width proxy).
    """
    wl = pd.to_numeric(dam_row.get("Weir_Length"), errors="coerce")
    if pd.notna(wl) and wl > 0:
        return float(wl), "csv_weir_length"

    sw = pd.to_numeric(dam_row.get("Spillway_Width_Ft"), errors="coerce")
    if pd.notna(sw) and sw > 0:
        return float(sw) * 0.3048, "nid_spillway_width_ft"

    if geom_row is not None:
        tw = pd.to_numeric(geom_row.get("owp_tw_bf"), errors="coerce")
        if pd.notna(tw) and tw > 0:
            return float(tw), "hydrofabric_bankfull_tw"

    return None, "none"


# ---------------------------------------------------------------------------
# Per-dam estimator
# ---------------------------------------------------------------------------

def process_dam(
    dam_row: pd.Series,
    vaa_df: pd.DataFrame,
    geom_df: pd.DataFrame,
    nwm_fdc: dict,
    staging_dir: Optional[Path],
    cache_dir: Path,
) -> dict:
    dam_id   = int(dam_row["OBJECTID"])
    lat      = float(dam_row["Latitude"])
    lon      = float(dam_row["Longitude"])
    flags: list[str] = []

    out = {col: None for col in OUTPUT_COLS}
    out["OBJECTID"] = dam_id
    out["method"]   = "lite_v1"

    # --- 0. Snap ComID ---
    snap_comid = _coerce_comid(dam_row.get("Reach_ID"))
    if snap_comid is None:
        snap_comid = _lookup_comid_direct(lon, lat)
    if snap_comid is None:
        flags.append("no_snap_comid")
        out["flags"] = "|".join(flags)
        return out
    out["comid_snap"] = snap_comid

    # --- 1. Adjacent ComIDs ---
    comid_up, comid_dn = _find_adjacent_comids(snap_comid, vaa_df)
    out["comid_up"] = comid_up
    out["comid_dn"] = comid_dn
    if comid_up is None:
        flags.append("no_comid_up")
    if comid_dn is None:
        flags.append("no_comid_dn")
        comid_dn = snap_comid  # use snap reach for SRC if no downstream found

    # --- 2. Flowline ---
    flowline_gdf = _load_or_fetch_flowline(dam_id, snap_comid, lat, lon, staging_dir)
    if flowline_gdf is None:
        flags.append("no_flowline")
        out["flags"] = "|".join(flags)
        return out

    # --- 3. DEM ---
    dem_paths, dem_res = _resolve_dem(dam_id, lat, lon, flowline_gdf, staging_dir, cache_dir)
    if dem_paths is None:
        if dem_res is not None and dem_res > MAX_DEM_RES_M:
            flags.append(f"dem_too_coarse_{dem_res:.0f}m")
        else:
            flags.append("no_dem")
        out["flags"] = "|".join(flags)
        return out
    out["dem_resolution_m"] = dem_res

    # --- 4. LiDAR date ---
    lidar_date = _get_lidar_date(lat, lon)
    out["lidar_date"] = lidar_date
    if lidar_date is None:
        flags.append("no_lidar_date")

    # --- 5. Channel geometry for downstream ComID ---
    geom_row = geom_df.loc[comid_dn] if comid_dn in geom_df.index else None
    if geom_row is None and snap_comid in geom_df.index:
        geom_row = geom_df.loc[snap_comid]
        flags.append("src_fallback_snap_comid")
    if geom_row is None:
        flags.append("no_hydrofabric_geometry")

    q_max_cms = fdc_dn.get("q100") if fdc_dn else None
    src = _build_src(geom_row, q_max_cms=q_max_cms) if geom_row is not None else None
    if src is None:
        flags.append("no_src")
    elif getattr(src, "method", None) == "trapezoid":
        flags.append("src_trapezoid_fallback")

    # --- 6. NWM FDC ---
    fdc_dn = nwm_fdc.get(comid_dn)
    if fdc_dn is None:
        flags.append("no_nwm_fdc")

    Q50 = _nwm_quantile(fdc_dn, 0.50) if fdc_dn else None
    out["Q50_cms"] = Q50

    # --- 7. Q_lidar from NWM retrospectives API ---
    Q_lidar: Optional[float] = None
    if lidar_date:
        Q_lidar = _fetch_q_lidar_api(comid_dn, lidar_date)
        if Q_lidar is None:
            flags.append("no_q_lidar_from_api")

    if Q_lidar is None and Q50 is not None:
        Q_lidar = Q50
        flags.append("Q_lidar_fallback_Q50")
    out["Q_lidar_cms"] = Q_lidar

    # --- 8. Width ---
    L_m, L_method = _estimate_width(dam_row, geom_row)
    out["L_m"]      = L_m
    out["L_method"] = L_method
    if L_m is None:
        flags.append("no_width")
        out["flags"] = "|".join(flags)
        return out
    if L_method != "csv_weir_length":
        flags.append(f"width_fallback_{L_method}")

    # --- 9. Longitudinal profile ---
    profile = _extract_profile(dem_paths, flowline_gdf, lat, lon, snap_comid)
    if profile is None:
        flags.append("no_profile")
        out["flags"] = "|".join(flags)
        return out

    # Check impoundment resolution: need ≥3 pixels upstream of dam
    up_pix = (profile["x_m"] < 0).sum()
    if up_pix < 3:
        flags.append("impoundment_lt3_pixels")

    # --- 10. Crest and downstream picks ---
    crest_row = _pick_crest(profile)
    if crest_row is None:
        flags.append("no_crest_cell")
        out["flags"] = "|".join(flags)
        return out

    ds_row = _pick_downstream(profile)
    if ds_row is None:
        flags.append("no_downstream_cell")
        out["flags"] = "|".join(flags)
        return out

    pool_WSE  = float(crest_row["DEM_Elev"])
    ds_WSE    = float(ds_row["DEM_Elev"])
    out["pool_WSE_m"]       = pool_WSE
    out["downstream_WSE_m"] = ds_WSE

    if pool_WSE <= ds_WSE:
        flags.append("pool_wse_le_downstream")

    # --- 11. Downstream bed ---
    ds_bed: Optional[float] = None
    if src is not None and Q_lidar is not None and Q_lidar > 0:
        stage_at_Q = np.interp(Q_lidar, src.Q, src.stages)
        ds_bed = ds_WSE - stage_at_Q
        out["downstream_bed_m"] = ds_bed
    else:
        flags.append("no_downstream_bed")

    # --- 12. Height ---
    if Q50 is not None and Q50 > 0 and L_m > 0:
        H_overflow = (Q50 / (Cd * L_m)) ** (2.0 / 3.0)
        crest_elev = pool_WSE - H_overflow
        out["H_overflow_m"] = H_overflow
        out["crest_elev_m"] = crest_elev

        if ds_bed is not None:
            dam_height = crest_elev - ds_bed
            out["dam_height_m"] = dam_height if dam_height > 0 else None
            if dam_height <= 0:
                flags.append("negative_dam_height")
        else:
            flags.append("no_dam_height_missing_bed")
    else:
        flags.append("no_dam_height_missing_Q50_or_L")

    out["flags"] = "|".join(flags) if flags else "ok"
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # --csv is kept as an old alias of --table so existing commands still parse.
    ap.add_argument("--table", "--csv", dest="table", type=Path, default=DEFAULT_TABLE,
                    help="Master dam table (.parquet) [default: data/full_lhd_website.parquet]")
    ap.add_argument("--output-csv",  type=Path,   default=DEFAULT_OUT)
    ap.add_argument("--staging-dir", type=Path,   default=None,
                    help="Root of an existing ARC staging tree (reuses STRM/ and DEM/)")
    ap.add_argument("--cache-dir",   type=Path,   default=DEFAULT_CACHE)
    ap.add_argument("--limit",       type=int,    default=100,
                    help="Max dams to process (default: 100)")
    ap.add_argument("--objectids",   type=int,    nargs="+", default=None,
                    help="Specific OBJECTIDs to process (overrides --limit)")
    args = ap.parse_args()

    if not args.table.exists():
        _log(f"ERROR: {args.table} not found"); return 1

    _log(f"Loading {args.table} …")
    df = read_dam_table(args.table)
    df["OBJECTID"] = pd.to_numeric(df["OBJECTID"], errors="coerce").astype("Int64")

    if args.objectids:
        df = df[df["OBJECTID"].isin(args.objectids)]
        _log(f"Targeting {len(df)} specified OBJECTIDs")
    else:
        # Default: first N dams that have ARC height results (for validation)
        has_arc = df["Dam_Height_GIS_Ft"].notna()
        df = df[has_arc].head(args.limit)
        _log(f"Targeting first {len(df)} dams with Dam_Height_GIS_Ft")

    df = df[df["Latitude"].notna() & df["Longitude"].notna()].reset_index(drop=True)
    _log(f"{len(df)} dams after lat/lon filter")

    # Load NHDPlus VAA (cached locally after first call)
    _log("Loading NHDPlus VAA table (one-time download if not cached) …")
    import pynhd as nhd
    vaa_df = nhd.nhdplus_vaa()
    _log(f"VAA: {len(vaa_df):,} rows")

    # Collect all unique downstream ComIDs for batch NWM fetch
    snap_comids = [_coerce_comid(r) for r in df["Reach_ID"]]
    needed_comids: list[int] = []
    for sc in snap_comids:
        if sc is None:
            continue
        _, cdn = _find_adjacent_comids(sc, vaa_df)
        for c in (sc, cdn):
            if c is not None and c not in needed_comids:
                needed_comids.append(c)

    if needed_comids:
        nwm_fdc = batch_fetch_nwm_fdc(needed_comids)
    else:
        nwm_fdc = {}
        _log("No ComIDs to fetch NWM FDC for")

    # Load hydrofabric geometry
    _log("Loading hydrofabric channel geometry …")
    geom_df = _load_channel_geometry(set(needed_comids))
    _log(f"Hydrofabric: {len(geom_df)} rows matched")

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    total = len(df)
    results = []
    for i, (_, row) in enumerate(df.iterrows(), 1):
        dam_id = int(row["OBJECTID"])
        _log(f"[{i}/{total}] Dam {dam_id} …")
        try:
            result = process_dam(row, vaa_df, geom_df, nwm_fdc, args.staging_dir, args.cache_dir)
        except Exception as e:
            _log(f"  ! Dam {dam_id} exception: {e}")
            result = {col: None for col in OUTPUT_COLS}
            result["OBJECTID"] = dam_id
            result["flags"]    = f"exception:{e}"
        results.append(result)

        # Write incrementally every 10 dams
        if i % 10 == 0 or i == total:
            out_df = pd.DataFrame(results, columns=OUTPUT_COLS)
            out_df.to_csv(args.output_csv, index=False)
            _log(f"  → wrote {i} rows to {args.output_csv}")

    out_df = pd.DataFrame(results, columns=OUTPUT_COLS)
    out_df.to_csv(args.output_csv, index=False)
    ok    = out_df["dam_height_m"].notna().sum()
    _log(f"\nDone. {ok}/{total} dams with a dam_height_m estimate → {args.output_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
