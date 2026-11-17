"""
Stage NHD flowlines and USGS 3DEP DEM tiles for the full LHD dataset.

Steps
-----
1. For each dam, fetch NHD flowlines (1 km upstream, 1.5 km downstream)
   and save as individual .gpkg files under STRM/{dam_id}/.
2. Query TNM for 1-m DEM tiles covering each flowline bbox
   (fallback: 1/9 arc-second, then 1/3 arc-second).
3. Record the dam → tile association in a manifest
   (tile_manifest.json + tile_manifest.csv).
4. Download all unique tiles to DEM/raw_3dep/.

Steps 1 + 2 run in parallel across dams; step 4 downloads in parallel.
Cached flowline .gpkg files and cached DEM tiles are reused automatically
(use --force-flowlines / --force-tiles to override).

Usage
-----
    python backend/stage_nhd_dem.py --staging-dir /data/lhd_staging

Optional flags
    --dams-table PATH     dam table (.parquet) [default: data/full_lhd_website.parquet]
                          (--dams-csv is accepted as an older name for this option)
    --limit N             process only the first N dams (for smoke-testing)
    --force-flowlines     refetch NHD flowlines even if a cached .gpkg exists
    --force-tiles         re-download DEM tiles even if already on disk
    --skip-download       build manifest but do not download tiles
    --workers N           parallel workers for flowline + tile-query stage [default: 8]
    --download-workers N  parallel workers for DEM tile downloads [default: 8]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import pynhd as nhd

from lhd_processor.download_geospatial_data import (
    download_nhd_flowline,
    query_dem_tiles,
    download_raw_tile,
)
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet

# NHD reach window: 1 km upstream, 1.5 km downstream
_FLOWLINE_DISTANCE_KM = (1.0, 1.5)

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_tile_manifest(staging_dir: Path) -> Tuple[Dict[int, List[str]], Dict[str, dict]]:
    """
    Load dam_tiles and tile_catalog from an existing tile_manifest.json.
    Returns empty dicts if the file doesn't exist or can't be parsed.
    """
    json_path = staging_dir / "tile_manifest.json"
    if not json_path.exists():
        return {}, {}
    try:
        with open(json_path) as f:
            payload = json.load(f)
        dam_tiles = {int(k): v for k, v in payload.get("dam_tiles", {}).items()}
        tile_catalog = payload.get("tile_catalog", {})
        return dam_tiles, tile_catalog
    except Exception as e:
        _log(f"Warning: could not load existing manifest ({e}) — will re-query all dams")
        return {}, {}

def _load_cached_flowline(site_dir: Path) -> Optional[dict]:
    """Return {path, gdf, comid} if a cached nhd_flowline_*.gpkg exists, else None."""
    if not site_dir.exists():
        return None
    gpkg_files = list(site_dir.glob("nhd_flowline_*.gpkg"))
    if not gpkg_files:
        return None

    import geopandas as gpd
    path = gpkg_files[0]
    try:
        gdf = gpd.read_file(path)
    except Exception as e:
        _log(f"  ! failed reading cached {path.name}: {e} — will refetch")
        return None

    comid = None
    m = re.search(r"nhd_flowline_(\d+)", path.stem)
    if m:
        comid = int(m.group(1))
    elif gdf is not None and not gdf.empty and "nhdplusid" in gdf.columns:
        comid = int(gdf.iloc[0]["nhdplusid"])

    return {"path": str(path), "gdf": gdf, "comid": comid}


# ---------------------------------------------------------------------------
# Per-dam worker: flowline + tile query
# ---------------------------------------------------------------------------

def _process_dam(
    idx: int,
    total: int,
    dam_id: int,
    lat: float,
    lon: float,
    flowline_dir: Path,
    force_flowlines: bool,
    vaa_df,
    cached_tiles: Optional[List[dict]],
) -> Tuple[int, dict, List[dict], bool]:
    """
    Resolve a single dam's flowline (cached or freshly downloaded) and query
    its DEM tiles. Returns (dam_id, flowline_entry, tiles, used_cache).

    cached_tiles: if not None, skip the TNM API call and use this list directly.
    """
    site_dir = flowline_dir / str(dam_id)

    flowline_entry: Optional[dict] = None
    used_cache = False
    if not force_flowlines:
        flowline_entry = _load_cached_flowline(site_dir)
        if flowline_entry is not None:
            used_cache = True
            _log(
                f"[{idx}/{total}] Dam {dam_id}: cached flowline "
                f"{Path(flowline_entry['path']).name}"
            )

    if flowline_entry is None:
        _log(f"[{idx}/{total}] Dam {dam_id}: fetching NHD flowlines ({lat:.5f}, {lon:.5f}) ...")
        path, gdf = download_nhd_flowline(
            lat, lon,
            flowline_dir=str(flowline_dir),
            distance_km=_FLOWLINE_DISTANCE_KM,
            site_id=dam_id,
            vaa_df=vaa_df,
        )
        comid: Optional[int] = None
        if path:
            m = re.search(r"nhd_flowline_(\d+)", Path(path).stem)
            if m:
                comid = int(m.group(1))
            elif gdf is not None and not gdf.empty and "nhdplusid" in gdf.columns:
                comid = int(gdf.iloc[0]["nhdplusid"])
            _log(f"[{idx}/{total}] Dam {dam_id}: → {path}")
        else:
            _log(f"[{idx}/{total}] Dam {dam_id}: → FAILED (no flowline)")
        flowline_entry = {"path": path, "gdf": gdf, "comid": comid}

    gdf = flowline_entry.get("gdf")
    if gdf is None or gdf.empty:
        return dam_id, flowline_entry, [], used_cache

    if cached_tiles is not None:
        _log(f"[{idx}/{total}] Dam {dam_id}: {len(cached_tiles)} tile(s) from manifest cache")
        return dam_id, flowline_entry, cached_tiles, used_cache

    tiles = query_dem_tiles(dam_id, gdf)
    if tiles:
        _log(
            f"[{idx}/{total}] Dam {dam_id}: {len(tiles)} tile(s) @ "
            f"{tiles[0]['resolution_m']} m"
        )
    else:
        _log(f"[{idx}/{total}] Dam {dam_id}: 0 tiles found")

    return dam_id, flowline_entry, tiles, used_cache


def load_vaa_df():
    """Load the NHDPlus VAA table (245 MB).

    HyRiver caches the underlying parquet locally, so the network download
    is one-time across the machine — but each call still pays a multi-second
    parquet read + DataFrame construct. When invoked inside a loop (e.g.
    one HUC batch at a time), the caller should load once and pass via
    ``stage_dams_parallel(..., vaa_df=...)`` to skip the repeat work.
    """
    _log("Pre-fetching NHDPlus VAA table (245 MB, once) ...")
    df = nhd.nhdplus_vaa()
    _log("VAA table ready.")
    return df


def stage_dams_parallel(
    dams_df: pd.DataFrame,
    flowline_dir: Path,
    staging_dir: Path,
    force_flowlines: bool,
    force_tile_query: bool,
    workers: int,
    vaa_df=None,
) -> Tuple[Dict[int, dict], Dict[int, List[str]], Dict[str, dict]]:
    """
    Run flowline fetch + DEM-tile query for each dam in parallel.

    ``vaa_df`` is the result of ``nhd.nhdplus_vaa()``. When ``None`` it's
    loaded inline; when provided (e.g. by a multi-HUC orchestrator) the
    inline load is skipped.

    Returns
    -------
    flowline_results : dam_id → {path, gdf, comid}
    manifest         : dam_id → [tile_filename, ...] (in dams_df order)
    tile_catalog     : filename → {url, dataset, resolution_m}
    """
    flowline_results: Dict[int, dict] = {}
    per_dam_tiles: Dict[int, List[str]] = {}
    tile_catalog: Dict[str, dict] = {}  # seeded from cache below, then updated by workers
    total = len(dams_df)

    if vaa_df is None:
        vaa_df = load_vaa_df()
    else:
        _log(f"Using preloaded NHDPlus VAA table ({len(vaa_df):,} rows).")

    cached_dam_tiles: Dict[int, List[str]] = {}
    cached_tile_catalog: Dict[str, dict] = {}
    if not force_tile_query:
        cached_dam_tiles, cached_tile_catalog = _load_tile_manifest(staging_dir)
        if cached_dam_tiles:
            n_cached = sum(1 for v in cached_dam_tiles.values() if v)
            _log(f"Loaded tile manifest: {n_cached} dams with cached tile associations.")
            tile_catalog.update(cached_tile_catalog)

    def _cached_tiles_for(dam_id: int) -> Optional[List[dict]]:
        """Reconstruct the tiles list for a dam from the cached manifest, or None to re-query."""
        if dam_id not in cached_dam_tiles:
            return None
        filenames = cached_dam_tiles[dam_id]
        if not filenames:
            return None  # previously got 0 tiles — re-query in case the API was flaky
        return [
            {"filename": fn, **cached_tile_catalog[fn]}
            for fn in filenames
            if fn in cached_tile_catalog
        ] or None

    jobs = []
    for i, row in dams_df.iterrows():
        jobs.append((
            i + 1,
            int(row["OBJECTID"]),
            float(row["Latitude"]),
            float(row["Longitude"]),
        ))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _process_dam,
                idx, total, dam_id, lat, lon,
                flowline_dir, force_flowlines, vaa_df,
                _cached_tiles_for(dam_id),
            ): dam_id
            for idx, dam_id, lat, lon in jobs
        }
        for fut in as_completed(futures):
            dam_id = futures[fut]
            try:
                dam_id, entry, tiles, _used_cache = fut.result()
            except Exception as e:
                _log(f"  ! Dam {dam_id} worker error: {e}")
                flowline_results[dam_id] = {"path": None, "gdf": None, "comid": None}
                per_dam_tiles[dam_id] = []
                continue

            flowline_results[dam_id] = entry
            per_dam_tiles[dam_id] = [t["filename"] for t in tiles]
            for t in tiles:
                tile_catalog.setdefault(t["filename"], {
                    "url": t["url"],
                    "dataset": t["dataset"],
                    "resolution_m": t["resolution_m"],
                })

    # Preserve dams_df ordering in manifest
    manifest: Dict[int, List[str]] = {
        int(row["OBJECTID"]): per_dam_tiles.get(int(row["OBJECTID"]), [])
        for _, row in dams_df.iterrows()
    }
    return flowline_results, manifest, tile_catalog


# ---------------------------------------------------------------------------
# Manifest output
# ---------------------------------------------------------------------------

def save_manifest(
    manifest: Dict[int, List[str]],
    tile_catalog: Dict[str, dict],
    flowline_results: Dict[int, dict],
    staging_dir: Path,
) -> None:
    """Write tile_manifest.json and tile_manifest.csv to staging_dir."""
    comid_map = {dam_id: fl.get("comid") for dam_id, fl in flowline_results.items()}

    payload = {
        "dam_tiles": {str(k): v for k, v in manifest.items()},
        "dam_comids": {str(k): v for k, v in comid_map.items()},
        "tile_catalog": tile_catalog,
    }
    json_path = staging_dir / "tile_manifest.json"
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Manifest saved: {json_path}")

    rows = []
    for dam_id, filenames in manifest.items():
        comid = comid_map.get(dam_id)
        for fn in filenames:
            meta = tile_catalog.get(fn, {})
            rows.append({
                "dam_id": dam_id,
                "nwm_comid": comid,
                "tile_filename": fn,
                "dataset": meta.get("dataset", ""),
                "resolution_m": meta.get("resolution_m", ""),
                "url": meta.get("url", ""),
            })

    csv_path = staging_dir / "tile_manifest.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"CSV manifest saved: {csv_path}")


# ---------------------------------------------------------------------------
# Parallel tile downloads
# ---------------------------------------------------------------------------

def download_all_tiles(
    tile_catalog: Dict[str, dict],
    raw_dem_dir: Path,
    workers: int,
    force: bool,
) -> Dict[str, str]:
    """
    Download every unique tile in tile_catalog, skipping those already on disk.

    Returns a rename map {original_catalog_filename: extracted_filename} for any
    tile whose on-disk name differs from the cataloged name (e.g. a .zip
    archive that was extracted to a .img/.tif). The caller is expected to use
    this to rewrite the manifest before saving.
    """
    todo: List[Tuple[str, dict]] = []
    cached = 0
    for filename, meta in tile_catalog.items():
        if not force and (raw_dem_dir / filename).exists():
            cached += 1
        else:
            todo.append((filename, meta))

    print(
        f"\nDEM tile downloads → {raw_dem_dir}: "
        f"{len(todo)} to fetch, {cached} already cached "
        f"(of {len(tile_catalog)} total)"
    )

    rename_map: Dict[str, str] = {}
    if not todo:
        print("Nothing to download.")
        return rename_map

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(download_raw_tile, meta["url"], str(raw_dem_dir)): filename
            for filename, meta in todo
        }
        done = 0
        for fut in as_completed(futures):
            filename = futures[fut]
            done += 1
            try:
                result = fut.result()
            except Exception as e:
                result = None
                _log(f"  [{done}/{len(todo)}] FAIL  {filename}: {e}")
            if result:
                ok += 1
                actual_name = Path(result).name
                if actual_name != filename:
                    rename_map[filename] = actual_name
                    _log(f"  [{done}/{len(todo)}] ok    {filename}  →  {actual_name}")
                else:
                    _log(f"  [{done}/{len(todo)}] ok    {filename}")
            else:
                # download_raw_tile already prints its own error; just count it.
                fail += 1

    print(
        f"\nTile downloads complete: {ok + cached} ok ({cached} cached), "
        f"{fail} failed"
    )
    return rename_map


def _apply_rename_map(
    rename_map: Dict[str, str],
    manifest: Dict[int, List[str]],
    tile_catalog: Dict[str, dict],
) -> None:
    """Rewrite manifest + tile_catalog in place to reflect post-extraction filenames."""
    if not rename_map:
        return
    for old, new in rename_map.items():
        meta = tile_catalog.pop(old, None)
        if meta is not None:
            tile_catalog[new] = meta
    for dam_id, filenames in manifest.items():
        manifest[dam_id] = [rename_map.get(fn, fn) for fn in filenames]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_stage(
    staging_dir: Path,
    dams_table: Path,
    *,
    limit: int | None = None,
    force_flowlines: bool = False,
    force_tiles: bool = False,
    force_tile_query: bool = False,
    skip_download: bool = False,
    workers: int = 8,
    download_workers: int = 8,
    vaa_df=None,
) -> None:
    """
    In-process entry point for the staging step. Same behavior as the CLI
    ``main()`` below — broken out so a multi-HUC orchestrator can call it
    directly and pass a pre-loaded ``vaa_df`` across batches.
    """
    flowline_dir = staging_dir / "STRM"
    raw_dem_dir = staging_dir / "DEM" / "raw_3dep"
    flowline_dir.mkdir(parents=True, exist_ok=True)
    raw_dem_dir.mkdir(parents=True, exist_ok=True)

    dams_df = read_dam_table(dams_table)
    n_raw = len(dams_df)
    dams_df = dams_df[
        dams_df["OBJECTID"].notna()
        & dams_df["Latitude"].notna()
        & dams_df["Longitude"].notna()
    ].reset_index(drop=True)
    dams_df["OBJECTID"] = dams_df["OBJECTID"].astype(int)
    if len(dams_df) < n_raw:
        print(f"Dropped {n_raw - len(dams_df)} row(s) with missing OBJECTID/lat/lon")
    if limit:
        dams_df = dams_df.head(limit)

    print(
        f"Processing {len(dams_df)} dams from {dams_table} "
        f"(workers={workers}, download_workers={download_workers})\n"
    )

    print("=" * 60)
    print("Steps 1 + 2 — NHD Flowlines + DEM Tile Manifest (parallel)")
    print("=" * 60)
    flowline_results, manifest, tile_catalog = stage_dams_parallel(
        dams_df, flowline_dir,
        staging_dir=staging_dir,
        force_flowlines=force_flowlines,
        force_tile_query=force_tile_query,
        workers=workers,
        vaa_df=vaa_df,
    )
    n_ok = sum(1 for v in flowline_results.values() if v.get("path"))
    n_tiles = sum(len(v) for v in manifest.values())
    n_unique = len(tile_catalog)
    print(
        f"\nFlowlines: {n_ok}/{len(dams_df)} succeeded — "
        f"tile references: {n_tiles} total, {n_unique} unique\n"
    )

    print("=" * 60)
    print("Step 3 — Saving Manifest")
    print("=" * 60)

    if not skip_download:
        # Save once now so a partial download still leaves a usable manifest,
        # then re-save after extraction in case zipped tiles were unpacked.
        save_manifest(manifest, tile_catalog, flowline_results, staging_dir)
        print()
        print("=" * 60)
        print("Step 4 — Downloading DEM Tiles")
        print("=" * 60)

        # On a partial-HUC retry after auto-prune, the manifest already
        # references every tile the HUC ever needed, but most of those tiles
        # are only needed by dams that either already cleared
        # build_trimmed_dems (cache check: dem_<id>.tif + constant_land.tif
        # + Manning_n.txt all exist) OR have been marked failed in a prior
        # run (dam_failures.json — build_trimmed_dems will skip them).
        # Skip downloading tiles ONLY referenced by such dams.
        dem_out_dir = staging_dir / "DEM"
        land_out_dir = staging_dir / "LAND"

        def _dam_trim_done(did: int) -> bool:
            return (
                (dem_out_dir / str(did) / f"dem_{did}.tif").exists()
                and (land_out_dir / str(did) / "constant_land.tif").exists()
                and (land_out_dir / str(did) / "Manning_n.txt").exists()
            )

        failed_ids: set = set()
        failures_path = staging_dir / "dam_failures.json"
        if failures_path.exists():
            try:
                with open(failures_path) as f:
                    failed_ids = {int(k) for k in json.load(f).keys()}
            except (OSError, json.JSONDecodeError, ValueError):
                failed_ids = set()

        needed_tiles: set = set()
        n_done = n_failed = n_pending = 0
        for did, fnames in manifest.items():
            if _dam_trim_done(did):
                n_done += 1
            elif did in failed_ids:
                n_failed += 1
            else:
                n_pending += 1
                needed_tiles.update(fnames)
        n_total_unique = len(tile_catalog)
        skipped = n_total_unique - len(needed_tiles)
        if skipped > 0:
            parts = []
            if n_done:
                parts.append(f"{n_done} dam(s) with cached trimmed DEM")
            if n_failed:
                parts.append(f"{n_failed} dam(s) marked failed")
            print(
                f"Skipping {skipped}/{n_total_unique} tile(s) — only "
                f"referenced by {' and '.join(parts)}. "
                f"{n_pending} dam(s) still need raw tiles."
            )
            download_catalog = {
                fn: meta for fn, meta in tile_catalog.items() if fn in needed_tiles
            }
        else:
            download_catalog = tile_catalog

        rename_map = download_all_tiles(
            download_catalog, raw_dem_dir,
            workers=download_workers,
            force=force_tiles,
        )
        if rename_map:
            print(f"\nRewriting manifest for {len(rename_map)} extracted zipped tiles")
            _apply_rename_map(rename_map, manifest, tile_catalog)
            save_manifest(manifest, tile_catalog, flowline_results, staging_dir)
    else:
        save_manifest(manifest, tile_catalog, flowline_results, staging_dir)
        print(f"\nSkipped tile download (--skip-download). {n_unique} unique tiles recorded.")

    print("\nDone.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--staging-dir", required=True, type=Path,
        help="Root directory where staged data will be written",
    )
    parser.add_argument(
        "--dams-table", "--dams-csv", dest="dams_table", type=Path, default=DEFAULT_DAMS_TABLE,
        help=f"Dam table, .parquet (default: {DEFAULT_DAMS_TABLE})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N dams (smoke-test mode)",
    )
    parser.add_argument(
        "--force-flowlines", action="store_true",
        help="Refetch NHD flowlines even if a cached .gpkg already exists.",
    )
    parser.add_argument(
        "--force-tiles", action="store_true",
        help="Re-download DEM tiles even if already present on disk.",
    )
    parser.add_argument(
        "--force-tile-query", action="store_true",
        help="Re-query the TNM API for tile associations even if cached in the manifest.",
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Build the manifest but do not download DEM tiles",
    )
    parser.add_argument(
        "--workers", type=int, default=8,
        help="Parallel workers for flowline + tile-query stage [default: 8]",
    )
    parser.add_argument(
        "--download-workers", type=int, default=8,
        help="Parallel workers for DEM tile downloads [default: 8]",
    )
    args = parser.parse_args()

    run_stage(
        staging_dir=args.staging_dir,
        dams_table=args.dams_table,
        limit=args.limit,
        force_flowlines=args.force_flowlines,
        force_tiles=args.force_tiles,
        force_tile_query=args.force_tile_query,
        skip_download=args.skip_download,
        workers=args.workers,
        download_workers=args.download_workers,
    )


if __name__ == "__main__":
    main()
