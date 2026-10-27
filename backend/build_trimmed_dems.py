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

import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge as rio_merge
from rasterio.warp import transform_bounds

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.land_raster import make_constant_land_raster

_X_SECTION_DIST_M = 500.0


def _mosaic_and_clip(tile_paths: List[str], bbox_wgs84, out_path: Path) -> None:
    """Mosaic DEM tiles and clip to a WGS84 bbox, keeping the tiles' CRS."""
    with rasterio.open(tile_paths[0]) as first:
        src_crs = first.crs
        profile = first.profile
    bounds = transform_bounds("EPSG:4326", src_crs, *bbox_wgs84, densify_pts=21)
    data, transform = rio_merge(tile_paths, bounds=bounds, resampling=Resampling.bilinear)
    profile.update(
        driver="GTiff",
        height=data.shape[1],
        width=data.shape[2],
        count=data.shape[0],
        transform=transform,
        compress="lzw",
    )
    for key in ("blockxsize", "blockysize", "tiled"):
        profile.pop(key, None)
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
    minx -= buffer_m
    miny -= buffer_m
    maxx += buffer_m
    maxy += buffer_m

    transformer = Transformer.from_crs(utm_epsg, 4326, always_xy=True)
    lon_min, lat_min = transformer.transform(minx, miny)
    lon_max, lat_max = transformer.transform(maxx, maxy)
    return lon_min, lat_min, lon_max, lat_max


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
