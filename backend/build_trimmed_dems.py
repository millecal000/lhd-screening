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

_X_SECTION_DIST_M = 500.0

# Guard against oversized flowline files. NLDI's upstreamTributaries navigation
# occasionally returns a whole river network (hundreds of segments spread over
# tens of km) instead of ~1 km of river. A DEM covering that area at 1 m needs
# tens of GB of memory and can exceed the classic-TIFF 4 GB limit. When the
# flowline bbox is wider or taller than _MAX_FLOWLINE_EXTENT_M, keep only the
# segments within _NEAR_SEED_REACH_M of the dam's own reach (the COMID in the
# file name). That still covers the dam reach and the reach just upstream,
# which is all the WSP step samples. Normal-sized flowlines are unchanged.
_MAX_FLOWLINE_EXTENT_M = 20_000.0
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
    """Mosaic DEM tiles and clip to a WGS84 bbox, keeping the tiles' CRS."""
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
    """flowline.total_bounds + buffer_m, computed in the local UTM zone, returned in WGS84."""
    gdf = gpd.read_file(gpkg_path)
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    centroid = gdf.geometry.union_all().centroid
    utm_epsg = _utm_epsg(centroid.x, centroid.y)
    gdf_utm = gdf.to_crs(epsg=utm_epsg)
    minx, miny, maxx, maxy = gdf_utm.total_bounds

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
            _log(
                f"  WARN {gpkg_path.name}: flowline spans {(maxx - minx) / 1000:.1f} x "
                f"{(maxy - miny) / 1000:.1f} km but the dam reach was not found in it; "
                f"using the full extent"
            )
    minx -= buffer_m
    miny -= buffer_m
    maxx += buffer_m
    maxy += buffer_m

    transformer = Transformer.from_crs(utm_epsg, 4326, always_xy=True)
    lon_min, lat_min = transformer.transform(minx, miny)
    lon_max, lat_max = transformer.transform(maxx, maxy)
    return lon_min, lat_min, lon_max, lat_max


def _segments_near_seed_reach(gdf_utm: gpd.GeoDataFrame, gpkg_path: Path):
    """Segments within _NEAR_SEED_REACH_M of the dam's own reach, or None.

    The dam's reach is the feature whose ID matches the COMID in the file name
    (nhd_flowline_<COMID>.gpkg). Returns None when that reach can't be found,
    so the caller falls back to the full flowline extent.
    """
    m = re.search(r"nhd_flowline_(\d+)", gpkg_path.stem)
    if not m:
        return None
    seed_comid = int(m.group(1))
    cols = {c.lower(): c for c in gdf_utm.columns}
    id_col = next((cols[c] for c in ("nhdplusid", "comid", "nhdplus_comid") if c in cols), None)
    if id_col is None:
        return None
    import pandas as pd  # already loaded by geopandas
    ids = pd.to_numeric(gdf_utm[id_col], errors="coerce")
    seed = gdf_utm[ids == seed_comid]
    if seed.empty:
        return None
    zone = seed.geometry.union_all().buffer(_NEAR_SEED_REACH_M)
    near = gdf_utm[gdf_utm.geometry.intersects(zone)]
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
