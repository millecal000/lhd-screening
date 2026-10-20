"""
HUC-batched NHD-WSP geometry pipeline (ARC-free replacement branch).

Same HUC batching, ledger, symlink-reuse, and file layout as rolling_pipeline.py.
Replaces the ARC steps with in-process DEM-profile sampling + energy balance.

For each HUC group in data/full_lhd_website.csv:
  1. Free-space gate — same as rolling_pipeline.py.
  2. Symlink existing DEM/STRM data from --existing-data-dir (reuses ARC-pipeline
     staging trees without copying).
  3. stage_nhd_dem  — fetch NHDPlus flowlines + query TNM tiles.
  4. build_trimmed_dems — warp raw tiles to per-dam trimmed DEMs.
  5. _run_wsp_batch — in-process ThreadPoolExecutor; per-dam WSP sampling +
     energy balance; writes WSP_RESULTS/{dam_id}/wsp_result.json.
  6. Write huc<level>_<KEY>/.READY_TO_ARCHIVE with counts + sizes.

Ledger lives at <local-staging-root>/wsp_ledger.json (separate from the ARC
pipeline's huc8_ledger.json so both can coexist in the same root).

Typical workflow
----------------
    python backend/run_wsp_pipeline.py \\
        --local-staging-root /data/lhd_wsp \\
        --existing-data-dir /data/lhd_staging \\
        --huc-level 6

    python backend/run_wsp_pipeline.py \\
        --local-staging-root /data/lhd_wsp --status

    python backend/run_wsp_pipeline.py \\
        --local-staging-root /data/lhd_wsp --aggregate

Other flags
-----------
    --huc-level {2,4,6,8}   batch granularity (default 6)
    --huc KEY               process only this group key
    --workers N             ThreadPoolExecutor workers (default 8)
    --min-free-gb N         free-space gate in GB (default 200)
    --existing-data-dir P   reuse DEM/STRM from this staging tree (repeatable)
    --reverse               walk HUC keys from highest down (lets two instances run in parallel)
    --status                print ledger and exit
    --aggregate             roll WSP_RESULTS into Dam_Height_WSP_Ft / Dam_Length_WSP_Ft on master CSV
    --locate DAM_ID         print HUC batch for a dam and exit
    --consolidate KEY       replace symlinks with real files for a bundle
    --mark-archived KEY     flip ledger status to archived
    --restore KEY|all       move archived bundle(s) back to local-staging-root
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from screening.wsp import (
    get_upstream_comid,
    pick_downstream_node,
    pick_upstream_node,
    sample_dem_along_flowlines,
)
from compute_wsp_danger_range import run as _run_wsp_danger_range
from lhd_processor.hydraulics import solve_weir_geom
from stage_nhd_dem import load_vaa_df, run_stage as run_stage_nhd_dem

import geopandas as gpd
import pynhd as nhd

_REPO_ROOT     = _BACKEND_ROOT.parent
DEFAULT_CSV    = _REPO_ROOT / "data" / "full_lhd_website.csv"
DEFAULT_FDC    = _REPO_ROOT / "frontend" / "data" / "nwm_fdc.json"
DEFAULT_SRC    = _REPO_ROOT / "frontend" / "data" / "synthetic_rating_curves.json"
_PARQUET_URL   = "lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet"
_PARQUET_CACHE = _BACKEND_ROOT / "cache" / "riverml_channel_geometry_with_ahg.parquet"

_SKIP_STATUSES = {"ready_to_archive", "archived"}
_REUSE_SUBDIRS = ("DEM", "STRM")

EXCLUDED_REVIEW_STATUSES = {
    "Removed",
    "Confirmed not a LHD",
    "Appears to not be LHD",
}

_EP50_IDX = 7   # index of p50 in [0,2,5,10,20,25,30,50,75,90,95,99,100]
M_TO_FT   = 3.28084


# ---------------------------------------------------------------------------
# Utilities (mirror rolling_pipeline.py)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def _huc_dirname(key: str) -> str:
    return f"huc{len(key)}_{key}"


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _load_ledger(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _save_ledger(path: Path, ledger: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _run_step(name: str, cmd: List[str]) -> None:
    print(f"\n  → {name}")
    print(f"    $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _build_existing_index(
    existing_dirs: List[Path],
) -> Dict[tuple, Path]:
    """Scan existing staging trees and return {(sub, dam_id): path}.

    Handles both layouts:
      flat:        existing_dir/DEM/{dam_id}/
      HUC-bundled: existing_dir/huc6_140600/DEM/{dam_id}/   ← ARC pipeline output
    """
    index: Dict[tuple, Path] = {}
    for src_root in existing_dirs:
        roots_to_scan = [src_root] + list(src_root.glob("huc*_*"))
        for root in roots_to_scan:
            for sub in _REUSE_SUBDIRS:
                sub_dir = root / sub
                if not sub_dir.is_dir():
                    continue
                for entry in sub_dir.iterdir():
                    try:
                        dam_id = int(entry.name)
                    except ValueError:
                        continue
                    if entry.is_dir():
                        index.setdefault((sub, dam_id), entry)
    return index


def _link_existing_data(
    huc_dir: Path,
    dam_ids: List[int],
    existing_index: Dict[tuple, Path],
) -> Dict[str, Dict[str, str]]:
    links: Dict[str, Dict[str, str]] = {}
    n_total = 0
    for did in dam_ids:
        per_dam: Dict[str, str] = {}
        for sub in _REUSE_SUBDIRS:
            target = huc_dir / sub / str(did)
            if target.exists() or target.is_symlink():
                continue
            src = existing_index.get((sub, did))
            if src is None:
                continue
            try:
                if any(src.iterdir()):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(src.resolve())
                    per_dam[sub] = str(src.resolve())
                    n_total += 1
            except OSError:
                continue
        if per_dam:
            links[str(did)] = per_dam
    if n_total:
        print(f"  Reused {n_total} subdir(s) across {len(links)} dam(s) via symlinks.")
    return links


def _prune_raw_dem_tiles(huc_dir: Path) -> Tuple[int, int]:
    """Delete DEM/raw_3dep/ tiles and stamp the deletion in tile_manifest.json.

    Returns (count_deleted, bytes_freed). Idempotent.
    """
    raw_dir = huc_dir / "DEM" / "raw_3dep"
    if not raw_dir.exists():
        return 0, 0
    count = total_bytes = 0
    for p in raw_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
            count += 1
    shutil.rmtree(raw_dir)
    manifest_path = huc_dir / "tile_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest["raw_tiles_deleted_at"] = _now()
            manifest["raw_tiles_deleted_count"] = count
            manifest["raw_tiles_deleted_bytes"] = total_bytes
            manifest_path.write_text(json.dumps(manifest, indent=2))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! could not stamp tile_manifest.json: {e}")
    return count, total_bytes


def _consolidate_huc(huc_dir: Path) -> Tuple[int, List[str]]:
    moved, dangling = 0, []
    for sub in _REUSE_SUBDIRS:
        sub_dir = huc_dir / sub
        if not sub_dir.is_dir():
            continue
        for entry in sub_dir.iterdir():
            if not entry.is_symlink():
                continue
            try:
                src = entry.resolve(strict=True)
            except FileNotFoundError:
                dangling.append(str(entry))
                continue
            entry.unlink()
            shutil.move(str(src), str(entry))
            moved += 1
    return moved, dangling


# ---------------------------------------------------------------------------
# Shared asset loading
# ---------------------------------------------------------------------------

def _load_fdc(fdc_path: Path) -> Dict[str, list]:
    if not fdc_path.exists():
        return {}
    with open(fdc_path) as f:
        raw = json.load(f)
    raw.pop("_meta", None)
    return raw


def _load_src(src_path: Path) -> Dict[str, dict]:
    """Load synthetic rating curves keyed by COMID string."""
    if not src_path.exists():
        return {}
    with open(src_path) as f:
        return json.load(f)


def _load_parquet(comids: set[int]) -> pd.DataFrame:
    if not _PARQUET_CACHE.exists():
        print("[wsp_pipeline] Downloading Lynker parquet (~188 MB, one-time) ...")
        import s3fs
        fs = s3fs.S3FileSystem(anon=True)
        _PARQUET_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fs.get(_PARQUET_URL, str(_PARQUET_CACHE))
    return (
        pq.read_table(
            _PARQUET_CACHE,
            columns=["feature_id", "owp_tw_bf", "owp_y_bf", "y_coef", "y_exp"],
            filters=[("feature_id", "in", list(comids))],
        )
        .to_pandas()
        .set_index("feature_id")
    )


# ---------------------------------------------------------------------------
# Per-dam WSP estimation
# ---------------------------------------------------------------------------

def _ep50(comid: int, fdc: Dict[str, list]) -> Optional[float]:
    vals = fdc.get(str(comid)) or fdc.get(f"{comid}.0")
    if not isinstance(vals, list) or len(vals) <= _EP50_IDX:
        return None
    v = float(vals[_EP50_IDX])
    return v if np.isfinite(v) and v > 0 else None


def _get_geom(gdf: gpd.GeoDataFrame, comid: int):
    rows = gdf[gdf["_cid"] == comid]
    return rows.iloc[0].geometry if not rows.empty else None


def _process_dam(
    dam_id: int,
    lat: float,
    lon: float,
    comid: int,
    huc_dir: Path,
    vaa_df: pd.DataFrame,
    geom_df: pd.DataFrame,
    fdc: Dict[str, list],
    src_dict: Dict[str, dict],
    search_up: float = 50.0,
) -> dict:
    """Run WSP geometry estimate for one dam. Returns a result dict."""
    result_dir = huc_dir / "WSP_RESULTS" / str(dam_id)
    result_path = result_dir / "wsp_result.json"
    if result_path.exists():
        with open(result_path) as f:
            data = json.load(f)
        data["_from_cache"] = True
        return data

    # Flowline
    strm_dir = huc_dir / "STRM" / str(dam_id)
    gpkg_files = list(strm_dir.glob("nhd_flowline_*.gpkg")) if strm_dir.exists() else []
    if not gpkg_files:
        return {"dam_id": dam_id, "status": "no_flowline"}

    gdf = gpd.read_file(gpkg_files[0])
    gdf.columns = gdf.columns.str.lower()
    id_col = next((c for c in ("nhdplusid", "comid", "nhdplus_comid") if c in gdf.columns), None)
    if id_col is None:
        return {"dam_id": dam_id, "status": "no_comid_col"}
    gdf["_cid"] = pd.to_numeric(gdf[id_col], errors="coerce").astype("Int64")

    dam_geom = _get_geom(gdf, comid)
    if dam_geom is None:
        return {"dam_id": dam_id, "status": "comid_not_in_gpkg"}

    upstream_comid = get_upstream_comid(comid, vaa_df)
    up_geom = _get_geom(gdf, upstream_comid) if upstream_comid else None

    # DEM
    dem_path = huc_dir / "DEM" / str(dam_id) / f"dem_{dam_id}.tif"
    if not dem_path.exists():
        return {"dam_id": dam_id, "status": "no_dem"}

    # WSP profile
    try:
        profile = sample_dem_along_flowlines(lat, lon, dam_geom, up_geom, dem_path)
    except Exception as e:
        return {"dam_id": dam_id, "status": f"profile_error:{e}"}

    if profile["elev_m"].notna().sum() < 3:
        return {"dam_id": dam_id, "status": "insufficient_dem_coverage"}

    # Reach slope from NHD VAA — used to find where downstream WS recovers to
    # normal gradient instead of picking an arbitrary fixed distance.
    reach_slope: Optional[float] = None
    vaa_row = vaa_df[vaa_df["comid"] == comid] if vaa_df is not None else pd.DataFrame()
    if not vaa_row.empty:
        s = float(vaa_row.iloc[0]["slope"])
        if s > 0 and s > -9990:
            reach_slope = s

    us_node = pick_upstream_node(profile, search_m=search_up)
    ds_node = pick_downstream_node(profile, reach_slope=reach_slope)
    wse_us   = float(us_node["elev_m"])
    wse_ds   = float(ds_node["elev_m"])
    delta_wse = wse_us - wse_ds

    if delta_wse <= 0 or not np.isfinite(delta_wse):
        return {"dam_id": dam_id, "status": "negative_delta_wse"}

    # Crest length: upstream reach owp_tw_bf, fall back to dam reach owp_tw_bf
    tw_bf = None
    tw_source = None
    for cid, label in ((upstream_comid, "upstream"), (comid, "dam_reach")):
        if cid is not None and cid in geom_df.index:
            v = float(geom_df.loc[cid, "owp_tw_bf"])
            if np.isfinite(v) and v > 0:
                tw_bf = v
                tw_source = label
                break
    if tw_bf is None:
        return {"dam_id": dam_id, "status": "no_tw_bf"}

    # Q: ep50 → fallback
    Q = _ep50(comid, fdc)
    if Q is None:
        Q = 1.0

    # y_T: tailwater depth = SRC stage interpolated at Q; fallback to raw AHG depth
    y_T = None
    src = src_dict.get(str(comid))
    if src:
        q_arr = src["discharge_cms"]
        s_arr = src["stage_m"]
        if q_arr and s_arr:
            y_T = float(np.interp(Q, q_arr, s_arr))
    if y_T is None and comid in geom_df.index:
        row = geom_df.loc[comid]
        y_coef = float(row["y_coef"])
        y_exp  = float(row["y_exp"])
        if np.isfinite(y_coef) and y_coef > 0 and np.isfinite(y_exp) and y_exp > 0:
            y_T = float(y_coef * Q ** y_exp)
    if y_T is None or y_T <= 0:
        y_T = max(delta_wse * 0.1, 0.1)

    # Energy balance
    H, P = solve_weir_geom(Q, tw_bf, y_T, delta_wse)
    if P is None or P <= 0 or P == -9999:
        return {"dam_id": dam_id, "status": "no_solution",
                "Q_cms": Q, "tw_bf_m": tw_bf, "delta_wse_m": delta_wse, "y_T_m": y_T}

    out = {
        "dam_id":          dam_id,
        "comid":           comid,
        "upstream_comid":  upstream_comid,
        "status":          "ok",
        "P_height_m":      round(float(P), 4),
        "H_head_m":        round(float(H), 4),
        "crest_length_m":  round(float(tw_bf), 4),
        "delta_wse_m":     round(float(delta_wse), 4),
        "y_T_m":           round(float(y_T), 4),
        "Q_cms":           round(float(Q), 4),
        "us_x_m":          round(float(us_node["x_m"]), 2),
        "ds_x_m":          round(float(ds_node["x_m"]), 2),
        "tw_source":       tw_source,
        "method":          "nhd_wsp",
        "completed_at":    _now(),
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(out, f, indent=2)
    return out


def _run_wsp_batch(
    huc_dir: Path,
    dams_subset: pd.DataFrame,
    vaa_df: pd.DataFrame,
    geom_df: pd.DataFrame,
    fdc: Dict[str, list],
    src_dict: Dict[str, dict],
    workers: int,
    search_up: float,
) -> Tuple[List[int], List[int]]:
    """Run WSP for every dam in dams_subset. Returns (ok_ids, failed_ids)."""
    def _result_path(did: int) -> Path:
        return huc_dir / "WSP_RESULTS" / str(did) / "wsp_result.json"

    def _cache_result(res: dict) -> None:
        """Write any result (ok or failed) so the dam is skipped on retry."""
        rp = _result_path(res["dam_id"])
        if not rp.exists():
            rp.parent.mkdir(parents=True, exist_ok=True)
            with open(rp, "w") as f:
                json.dump(res, f, indent=2)

    ok: List[int] = []
    failed: List[int] = []
    jobs = []

    # Pre-scan: classify already-cached dams and build jobs only for new ones.
    for _, row in dams_subset.iterrows():
        dam_id = int(row["OBJECTID"])
        rp = _result_path(dam_id)
        if rp.exists():
            with open(rp) as f:
                data = json.load(f)
            (ok if data.get("status") == "ok" else failed).append(dam_id)
            continue
        if pd.isna(row.get("Reach_ID")):
            failed.append(dam_id)
            _cache_result({"dam_id": dam_id, "status": "no_comid"})
            continue
        jobs.append((dam_id, float(row["Latitude"]), float(row["Longitude"]),
                     int(float(row["Reach_ID"]))))

    n_pre = len(ok) + len(failed)
    if n_pre:
        print(f"  Pre-cached: {len(ok)} ok, {len(failed)} failed — skipping.")

    n = len(jobs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _process_dam, dam_id, lat, lon, comid,
                huc_dir, vaa_df, geom_df, fdc, src_dict, search_up
            ): dam_id
            for dam_id, lat, lon, comid in jobs
        }
        for i, fut in enumerate(as_completed(futures), 1):
            dam_id = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [{i}/{n}] Dam {dam_id}: EXCEPTION — {e}")
                failed.append(dam_id)
                _cache_result({"dam_id": dam_id, "status": f"exception:{e}"})
                continue
            status = res.get("status", "?")
            if status == "ok":
                ok.append(dam_id)
                if i % 50 == 0 or i == n:
                    print(f"  [{i}/{n}] Dam {dam_id}: ok  "
                          f"P={res['P_height_m']:.2f} m  L={res['crest_length_m']:.1f} m")
            else:
                failed.append(dam_id)
                print(f"  [{i}/{n}] Dam {dam_id}: {status}")
                _cache_result(res)

    return ok, failed


def _tally_wsp(huc_dir: Path, dam_ids: List[int]) -> Tuple[List[int], List[int]]:
    results_dir = huc_dir / "WSP_RESULTS"
    ok, failed = [], []
    for did in dam_ids:
        p = results_dir / str(did) / "wsp_result.json"
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            if data.get("status") == "ok":
                ok.append(did)
            else:
                failed.append(did)
        else:
            failed.append(did)
    return ok, failed


# ---------------------------------------------------------------------------
# HUC batch processor
# ---------------------------------------------------------------------------

def _process_huc(
    key: str,
    dams_subset: pd.DataFrame,
    local_root: Path,
    ledger: Dict[str, dict],
    ledger_path: Path,
    workers: int,
    existing_index: Dict[tuple, Path],
    vaa_df,
    geom_df: pd.DataFrame,
    fdc: Dict[str, list],
    src_dict: Dict[str, dict],
    search_up: float,
) -> None:
    label = f"HUC{len(key)} {key}"
    huc_dir = local_root / _huc_dirname(key)
    huc_dir.mkdir(parents=True, exist_ok=True)
    dams_csv = huc_dir / "dams.csv"
    dams_subset.drop(columns=["_GROUP"], errors="ignore").to_csv(dams_csv, index=False)
    dam_ids = [int(x) for x in dams_subset["OBJECTID"].tolist()]

    entry = ledger.setdefault(key, {})
    entry.update({"huc_level": len(key), "dam_ids": dam_ids,
                  "status": "staging", "staged_at": _now()})

    external_links = _link_existing_data(huc_dir, dam_ids, existing_index)
    if external_links:
        prev = entry.get("external_links", {})
        prev.update(external_links)
        entry["external_links"] = prev
    _save_ledger(ledger_path, ledger)

    py      = sys.executable
    backend = str(_BACKEND_ROOT)

    # Only stage dams that don't already have a wsp_result.json — avoids
    # re-downloading DEMs for dams that are already done or permanently failed.
    results_dir = huc_dir / "WSP_RESULTS"
    pending_ids = [
        did for did in dam_ids
        if not (results_dir / str(did) / "wsp_result.json").exists()
    ]

    # Of those, only re-run staging for dams whose trimmed DEM is missing.
    # Raw 3DEP tiles are pruned after each batch, but trimmed DEMs persist —
    # so dams with an existing dem_{id}.tif can skip straight to _run_wsp_batch.
    needs_staging_ids = [
        did for did in pending_ids
        if not (huc_dir / "DEM" / str(did) / f"dem_{did}.tif").exists()
    ]
    n_dem_cached = len(pending_ids) - len(needs_staging_ids)

    if pending_ids and len(pending_ids) < len(dam_ids):
        print(f"  {len(dam_ids) - len(pending_ids)} dams already have results — skipping.")
    if n_dem_cached:
        print(f"  {n_dem_cached} pending dams already have trimmed DEMs — skipping staging.")

    if needs_staging_ids:
        stage_csv = huc_dir / "dams_needs_staging.csv"
        dams_subset[dams_subset["OBJECTID"].astype(int).isin(needs_staging_ids)] \
            .drop(columns=["_GROUP"], errors="ignore") \
            .to_csv(stage_csv, index=False, encoding="latin-1")
    elif pending_ids:
        stage_csv = None  # all pending dams have DEMs; skip staging entirely
    else:
        stage_csv = dams_csv

    stage_args = ["--staging-dir", str(huc_dir), "--dams-csv", str(stage_csv),
                  "--workers", str(workers), "--download-workers", str(workers)]

    if stage_csv is not None:
        # stage_nhd_dem — in-process if vaa_df already loaded, else subprocess
        if vaa_df is not None:
            print(f"\n  → stage_nhd_dem (in-process, {len(needs_staging_ids)} dams)")
            run_stage_nhd_dem(
                staging_dir=huc_dir, dams_csv=stage_csv,
                workers=workers, download_workers=workers, vaa_df=vaa_df,
            )
        else:
            _run_step("stage_nhd_dem",
                      [py, f"{backend}/stage_nhd_dem.py"] + stage_args)

        _run_step("build_trimmed_dems",
                  [py, f"{backend}/build_trimmed_dems.py",
                   "--staging-dir", str(huc_dir), "--workers", str(workers),
                   "--no-land"])
    else:
        print(f"\n  → stage_nhd_dem / build_trimmed_dems skipped (all DEMs cached)")

    pruned, freed = _prune_raw_dem_tiles(huc_dir)
    if pruned:
        print(f"\n  Pruned {pruned} raw 3DEP tile(s); freed {freed / 1e9:.1f} GB.")

    entry["status"] = "wsp_running"
    entry["wsp_at"] = _now()
    _save_ledger(ledger_path, ledger)

    print(f"\n  → run_wsp_batch ({len(dam_ids)} dams, {workers} workers)")
    ok, failed = _run_wsp_batch(
        huc_dir, dams_subset, vaa_df, geom_df, fdc, src_dict,
        workers, search_up,
    )

    size_bytes = _dir_size_bytes(huc_dir)
    # Promote to ready_to_archive if every failure has a cached result file
    # (meaning all failures are permanent — retrying won't help).
    # Keep "partial" only when the batch was interrupted before some dams ran.
    all_failures_filed = all(
        (huc_dir / "WSP_RESULTS" / str(did) / "wsp_result.json").exists()
        for did in failed
    )
    status = "ready_to_archive" if (not failed or all_failures_filed) else "partial"
    marker = {
        "huc": key, "huc_level": len(key), "status": status,
        "dam_count": len(dam_ids), "ok": ok, "failed": failed,
        "size_bytes": size_bytes, "size_human": f"{size_bytes / 1e9:.1f} GB",
        "completed_at": _now(),
        "uses_external_data": bool(entry.get("external_links")),
        "external_paths": sorted({
            p for per in entry.get("external_links", {}).values()
            for p in per.values()
        }),
    }
    (huc_dir / ".READY_TO_ARCHIVE").write_text(json.dumps(marker, indent=2))
    entry.update({k: v for k, v in marker.items() if k != "external_paths"})
    _save_ledger(ledger_path, ledger)

    print(f"\n{label}: {status}  ({len(ok)} ok / {len(failed)} failed, {marker['size_human']})")


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _update_csv(args: argparse.Namespace, src_dict: Dict[str, dict] | None = None) -> None:
    """Aggregate WSP geometry into the CSV, then compute NWM danger ranges."""
    print("\nAggregating WSP geometry into master CSV …")
    try:
        _cmd_aggregate(args)
    except Exception as e:
        print(f"  WARN aggregate failed: {e}")

    print("Computing NWM danger ranges …")
    try:
        _run_wsp_danger_range(csv_path=args.dams_csv, src_data=src_dict)
    except Exception as e:
        print(f"  WARN danger range computation failed: {e}")


def _cmd_run(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    local_root.mkdir(parents=True, exist_ok=True)
    ledger_path = local_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)

    existing_dirs = [Path(p).resolve() for p in (args.existing_data_dir or [])]
    for d in existing_dirs:
        if not d.is_dir():
            sys.exit(f"--existing-data-dir does not exist: {d}")

    if not args.dams_csv.exists():
        sys.exit(f"Dam CSV not found: {args.dams_csv}\nRun: python backend/assign_huc8.py")
    dams = pd.read_csv(args.dams_csv, low_memory=False, encoding="latin-1")
    if "HUC8" not in dams.columns:
        sys.exit(f"{args.dams_csv} has no HUC8 column.\nRun: python backend/assign_huc8.py")

    dams = dams[
        dams["OBJECTID"].notna() & dams["Latitude"].notna() &
        dams["Longitude"].notna() & dams["HUC8"].notna()
    ].copy()
    dams["OBJECTID"] = dams["OBJECTID"].astype(int)
    dams["HUC8"] = dams["HUC8"].astype(str).str.split(".").str[0].str.zfill(8)
    dams = dams[dams["HUC8"] != "00000000"]

    if "Review_Status" in dams.columns:
        status_col = dams["Review_Status"].fillna("").astype(str).str.strip()
        excl = status_col.isin(EXCLUDED_REVIEW_STATUSES)
        n_excl = int(excl.sum())
        if n_excl:
            print(f"Excluding {n_excl} non-LHD dams")
            dams = dams[~excl]

    level = args.huc_level
    dams["_GROUP"] = dams["HUC8"].str[:level]
    groups = sorted(dams.groupby("_GROUP"), key=lambda kv: kv[0])

    if args.huc:
        groups = [(h, g) for h, g in groups if h == args.huc]
        if not groups:
            sys.exit(f"No dams for group '{args.huc}' at HUC level {level}")

    skip = {"archived"} if args.huc else _SKIP_STATUSES
    pending = [(h, g) for h, g in groups
               if ledger.get(h, {}).get("status") not in skip]

    if args.reverse:
        pending = list(reversed(pending))

    n_partial = sum(1 for h, _ in pending if ledger.get(h, {}).get("status") == "partial")
    print(f"HUC{level} batches: {len(groups)} total | "
          f"{len(groups) - len(pending)} done | "
          f"{len(pending)} pending ({n_partial} partial being retried)"
          + (" | reversed" if args.reverse else ""))

    # Load SRC data early so it's available for danger range computation
    # even in the "nothing to do" path.
    src_dict = _load_src(DEFAULT_SRC)
    print(f"  SRC loaded: {len(src_dict):,} reaches")

    if not pending:
        print("Nothing to do.")
        _update_csv(args, src_dict=src_dict)
        return

    # Build existing-data index once — handles both flat and HUC-bundled layouts
    existing_index: Dict[tuple, Path] = {}
    if existing_dirs:
        print(f"\nIndexing existing data from: {', '.join(str(d) for d in existing_dirs)} ...")
        existing_index = _build_existing_index(existing_dirs)
        n_dem  = sum(1 for sub, _ in existing_index if sub == "DEM")
        n_strm = sum(1 for sub, _ in existing_index if sub == "STRM")
        print(f"  Found {n_dem} DEM dirs, {n_strm} STRM dirs across existing trees.")

    # --- Load remaining shared assets ---
    print("\nLoading shared assets ...")

    vaa_df = None
    try:
        vaa_df = load_vaa_df()
    except Exception as e:
        print(f"  ! VAA preload failed: {e} — will reload per HUC (slower)")

    fdc = _load_fdc(DEFAULT_FDC)
    print(f"  FDC loaded: {len(fdc):,} reaches")

    # Collect all COMIDs (dam + upstream) for parquet preload
    all_comids: set[int] = set()
    for _, g in pending:
        for comid_raw in g["Reach_ID"].dropna():
            try:
                cid = int(float(comid_raw))
                all_comids.add(cid)
                if vaa_df is not None:
                    up = get_upstream_comid(cid, vaa_df)
                    if up:
                        all_comids.add(up)
            except (ValueError, TypeError):
                pass
    print(f"  Loading Lynker parquet for {len(all_comids):,} COMIDs ...")
    geom_df = _load_parquet(all_comids)
    print(f"  Parquet loaded: {len(geom_df):,} rows")

    # --- Main HUC loop ---
    for key, group in pending:
        free = _free_gb(local_root)
        if free < args.min_free_gb:
            print(f"\nFree space {free:.1f} GB < {args.min_free_gb} GB threshold — stopping.")
            print("Archive a ready bundle, then rerun.")
            _update_csv(args, src_dict=src_dict)
            return

        print(f"\n{'=' * 60}")
        print(f"HUC{level} {key}  ({len(group)} dams, {free:.1f} GB free)")
        print(f"{'=' * 60}")
        try:
            _process_huc(
                key, group, local_root, ledger, ledger_path,
                args.workers, existing_index, vaa_df, geom_df, fdc, src_dict,
                args.search_up,
            )
        except Exception as e:
            print(f"\nFATAL: HUC{level} {key} failed: {e}")
            traceback.print_exc()
            entry = ledger.setdefault(key, {})
            entry.update({"status": "errored", "error": str(e), "errored_at": _now()})
            _save_ledger(ledger_path, ledger)
            print("Halting. Investigate and rerun.")
            return

        _update_csv(args, src_dict=src_dict)

    print("\nAll pending HUCs processed.")
    _update_csv(args, src_dict=src_dict)


def _cmd_status(args: argparse.Namespace) -> None:
    ledger_path = args.local_staging_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        print(f"No ledger at {ledger_path}")
        return
    counts: Dict[str, int] = {}
    rows = []
    for key, entry in sorted(ledger.items()):
        s = entry.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        rows.append((key, s,
                     entry.get("dam_count", len(entry.get("dam_ids", []))),
                     len(entry.get("failed", [])),
                     entry.get("size_human", "")))
    print(f"Ledger: {ledger_path}\n")
    print(f"{'HUC':<10} {'status':<18} {'dams':>5} {'failed':>7} {'size':>10}")
    print("-" * 55)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<18} {r[2]:>5} {r[3]:>7} {r[4]:>10}")
    print("\nTotals:")
    for s in sorted(counts):
        print(f"  {s:<22} {counts[s]}")


def _cmd_aggregate(args: argparse.Namespace) -> None:
    """Roll WSP_RESULTS into Dam_Height_WSP_Ft / Dam_Length_WSP_Ft on the master CSV."""
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")

    search_roots: List[Path] = [args.local_staging_root.resolve()]
    for d in (args.existing_data_dir or []):
        p = Path(d).resolve()
        if p.is_dir():
            search_roots.append(p)

    print(f"Scanning {len(search_roots)} root(s) for wsp_result.json ...")
    seen: Dict[int, Path] = {}
    for root in search_roots:
        for p in root.rglob("wsp_result.json"):
            try:
                dam_id = int(p.parent.name)
            except ValueError:
                continue
            seen.setdefault(dam_id, p)
    print(f"  → {len(seen)} dam results found")
    if not seen:
        print("Nothing to aggregate.")
        return

    rows: Dict[int, Dict] = {}
    for dam_id, p in seen.items():
        try:
            with open(p) as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if r.get("status") != "ok":
            continue
        rows[dam_id] = {
            "Dam_Height_WSP_Ft": round(float(r["P_height_m"]) * M_TO_FT, 2),
            "Dam_Length_WSP_Ft": round(float(r["crest_length_m"]) * M_TO_FT, 2),
            "WSP_Q_cms":         r["Q_cms"],
            "WSP_delta_wse_m":   r["delta_wse_m"],
            "WSP_upstream_comid": r.get("upstream_comid"),
        }
    print(f"  {len(rows)} dams with ok results")

    dams = pd.read_csv(dams_csv, low_memory=False, encoding="latin-1")
    wsp_cols = ["Dam_Height_WSP_Ft", "Dam_Length_WSP_Ft",
                "WSP_Q_cms", "WSP_delta_wse_m", "WSP_upstream_comid"]
    for col in wsp_cols + ["Qmin_env", "Qmax_env"]:
        if col not in dams.columns:
            dams[col] = np.nan

    # Update only the dams that have a result in the scanned folders. Every
    # other dam keeps its existing values, so results from bundles archived to
    # another drive, or HUCs run on another computer, are not wiped.
    oid = pd.to_numeric(dams["OBJECTID"], errors="coerce")
    hit = oid.isin(list(rows))
    geom_cols = ["Dam_Height_WSP_Ft", "Dam_Length_WSP_Ft"]
    before = dams.loc[hit, geom_cols].copy()
    for col in wsp_cols:
        dams.loc[hit, col] = [rows[int(i)][col] for i in oid[hit]]

    # If a dam's height or length changed, clear its danger range so
    # compute_wsp_danger_range (which skips dams that already have Qmin_env)
    # recomputes it from the new geometry.
    changed = (before != dams.loc[hit, geom_cols]).any(axis=1)
    stale = changed[changed].index
    dams.loc[stale, ["Qmin_env", "Qmax_env"]] = np.nan

    dams.to_csv(dams_csv, index=False)
    kept = int((~hit & dams["Dam_Height_WSP_Ft"].notna()).sum())
    print(f"Updated {int(hit.sum())} dams in {dams_csv} "
          f"({len(stale)} with new geometry; {kept} other dams kept as-is)")


def _cmd_locate(args: argparse.Namespace) -> None:
    target = int(args.locate)
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")
    dams = pd.read_csv(dams_csv, low_memory=False, encoding="latin-1")
    if "HUC8" not in dams.columns:
        sys.exit("No HUC8 column — run assign_huc8.py first")
    row = dams[dams["OBJECTID"] == target]
    if row.empty:
        sys.exit(f"OBJECTID {target} not found")
    huc8 = str(row.iloc[0]["HUC8"]).split(".")[0].zfill(8)
    key = huc8[:args.huc_level]
    print(f"Dam {target} → HUC8 {huc8}  (HUC{args.huc_level} batch {key}, "
          f"folder {_huc_dirname(key)})")
    ledger = _load_ledger(args.local_staging_root / "wsp_ledger.json")
    entry = ledger.get(key, {})
    if entry:
        print(f"  ledger status: {entry.get('status', '?')}")


def _cmd_consolidate(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    key = args.consolidate
    huc_dir = local_root / _huc_dirname(key)
    if not huc_dir.is_dir():
        sys.exit(f"HUC dir not found: {huc_dir}")
    print(f"Consolidating {huc_dir} ...")
    moved, dangling = _consolidate_huc(huc_dir)
    print(f"  Moved {moved} symlinked dir(s).")
    if dangling:
        print(f"  {len(dangling)} dangling symlinks skipped.")
    entry = ledger.setdefault(key, {})
    entry.update({"consolidated": True, "consolidated_at": _now(),
                  "consolidated_moves": moved})
    entry.pop("external_links", None)
    _save_ledger(ledger_path, ledger)
    print(f"Done. Safe to move {huc_dir}.")


def _cmd_mark_archived(args: argparse.Namespace) -> None:
    ledger_path = args.local_staging_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    key = args.mark_archived
    if key not in ledger:
        sys.exit(f"'{key}' not in ledger")
    ledger[key].update({"status": "archived", "archived_at": _now()})
    _save_ledger(ledger_path, ledger)
    print(f"HUC group {key} marked archived.")


def _cmd_prune_raw_all(args: argparse.Namespace) -> None:
    """Delete DEM/raw_3dep/ from every bundle in the ledger."""
    local_root: Path = args.local_staging_root
    ledger = _load_ledger(local_root / "wsp_ledger.json")
    if not ledger:
        sys.exit(f"No ledger at {local_root / 'wsp_ledger.json'}")

    total_files = total_bytes = 0
    pruned_keys: List[str] = []
    for key in sorted(ledger):
        huc_dir = local_root / _huc_dirname(key)
        if not huc_dir.is_dir():
            continue
        count, freed = _prune_raw_dem_tiles(huc_dir)
        if count:
            print(f"[{key}] deleted {count} tile(s); freed {freed / 1e9:.2f} GB")
            total_files += count
            total_bytes += freed
            pruned_keys.append(key)

    print(f"\nDone. {len(pruned_keys)} bundle(s); "
          f"{total_files} tile(s); freed {total_bytes / 1e9:.1f} GB total.")


def _cmd_archive_to(args: argparse.Namespace) -> None:
    """Consolidate + move + mark-archived for every ready_to_archive bundle."""
    local_root: Path = args.local_staging_root
    dest: Path = args.archive_to.resolve()
    if not dest.is_dir():
        sys.exit(f"--archive-to directory does not exist: {dest}")

    ledger_path = local_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        sys.exit(f"No ledger at {ledger_path}")

    statuses = {"ready_to_archive"}
    if args.include_partial:
        statuses.add("partial")
    ready = [k for k, v in ledger.items() if v.get("status") in statuses]
    if not ready:
        print(f"No bundles in {statuses} state. Nothing to do.")
        return

    print(f"Archiving {len(ready)} bundle(s) → {dest}")
    archived: List[str] = []
    failed: List[Tuple[str, str]] = []
    for key in sorted(ready):
        huc_dir = local_root / _huc_dirname(key)
        if not huc_dir.is_dir():
            failed.append((key, f"bundle missing at {huc_dir}"))
            continue
        try:
            print(f"\n[{key}] consolidating symlinks …")
            moved, dangling = _consolidate_huc(huc_dir)
            print(f"  moved {moved} symlinked dir(s)"
                  + (f"; {len(dangling)} dangling skipped" if dangling else ""))

            pruned, freed = _prune_raw_dem_tiles(huc_dir)
            if pruned:
                print(f"  deleted {pruned} raw 3DEP tile(s); freed {freed / 1e9:.1f} GB")

            entry = ledger.setdefault(key, {})
            entry.update({"consolidated": True, "consolidated_at": _now(),
                          "consolidated_moves": moved})
            entry.pop("external_links", None)

            size_bytes = _dir_size_bytes(huc_dir)
            print(f"[{key}] moving {size_bytes / 1e9:.1f} GB → {dest / huc_dir.name} …")
            shutil.move(str(huc_dir), str(dest / huc_dir.name))

            entry.update({"status": "archived", "archived_at": _now(),
                          "archived_to": str(dest / huc_dir.name)})
            _save_ledger(ledger_path, ledger)
            print(f"[{key}] archived.")
            archived.append(key)
        except Exception as e:
            _save_ledger(ledger_path, ledger)
            print(f"[{key}] FAILED: {e}")
            failed.append((key, str(e)))

    print(f"\nDone. archived={len(archived)} failed={len(failed)}")
    if failed:
        for k, why in failed:
            print(f"  ! {k}: {why}")


def _cmd_restore(args: argparse.Namespace) -> None:
    """Move archived bundles back to local_staging_root and reset ledger status."""
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        sys.exit(f"No ledger at {ledger_path}")

    keys_arg: Optional[str] = args.restore
    if keys_arg and keys_arg.lower() != "all":
        targets = [k.strip() for k in keys_arg.split(",")]
        missing = [k for k in targets if k not in ledger]
        if missing:
            sys.exit(f"Keys not in ledger: {', '.join(missing)}")
        to_restore = targets
    else:
        to_restore = [k for k, v in ledger.items() if v.get("status") == "archived"]

    if not to_restore:
        print("No archived bundles to restore.")
        return

    print(f"Restoring {len(to_restore)} bundle(s) → {local_root}")
    restored, failed = [], []
    for key in sorted(to_restore):
        entry = ledger[key]
        archived_to = entry.get("archived_to")
        if not archived_to:
            failed.append((key, "no archived_to in ledger"))
            continue
        src = Path(archived_to)
        if not src.is_dir():
            failed.append((key, f"source not found: {src}"))
            continue
        dest = local_root / _huc_dirname(key)
        if dest.exists():
            failed.append((key, f"destination already exists: {dest}"))
            continue
        try:
            size_bytes = _dir_size_bytes(src)
            print(f"[{key}] moving {size_bytes / 1e9:.1f} GB → {dest} …")
            shutil.move(str(src), str(dest))
            entry.update({"status": "ready_to_archive"})
            for field in ("archived_at", "archived_to"):
                entry.pop(field, None)
            _save_ledger(ledger_path, ledger)
            print(f"[{key}] restored.")
            restored.append(key)
        except Exception as e:
            _save_ledger(ledger_path, ledger)
            print(f"[{key}] FAILED: {e}")
            failed.append((key, str(e)))

    print(f"\nDone. restored={len(restored)} failed={len(failed)}")
    if failed:
        for k, why in failed:
            print(f"  ! {k}: {why}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--local-staging-root", type=Path, required=True)
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--huc-level", type=int, choices=[2, 4, 6, 8], default=6)
    parser.add_argument("--min-free-gb", type=float, default=200.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--existing-data-dir", type=Path, action="append", default=None)
    parser.add_argument("--huc", type=str, default=None, metavar="KEY")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--search-up", type=float, default=50.0,
                        help="Upstream flat-zone search window (m) [50]")
    parser.add_argument("--status",       action="store_true")
    parser.add_argument("--aggregate",    action="store_true")
    parser.add_argument("--locate",       type=str, default=None, metavar="DAM_ID")
    parser.add_argument("--consolidate",  type=str, default=None, metavar="KEY")
    parser.add_argument("--mark-archived",type=str, default=None, metavar="KEY")
    parser.add_argument("--archive-to",      type=Path, default=None, metavar="PATH",
                        help="Consolidate + prune raw tiles + move + mark-archived "
                             "for every ready_to_archive bundle.")
    parser.add_argument("--include-partial", action="store_true",
                        help="With --archive-to: also move partial bundles "
                             "(some dams failed) in addition to ready_to_archive ones.")
    parser.add_argument("--prune-raw-all",   action="store_true",
                        help="Delete DEM/raw_3dep/ tiles from every bundle in the "
                             "ledger to reclaim disk space. Does not move or archive.")
    parser.add_argument("--restore", type=str, default=None, metavar="KEY|all",
                        help="Move archived bundle(s) back to local-staging-root and "
                             "reset ledger status to ready_to_archive. Pass a single "
                             "HUC key, a comma-separated list, or 'all'.")
    args = parser.parse_args()

    if args.status:
        _cmd_status(args)
    elif args.locate:
        _cmd_locate(args)
    elif args.prune_raw_all:
        _cmd_prune_raw_all(args)
    elif args.archive_to:
        _cmd_archive_to(args)
    elif args.consolidate:
        _cmd_consolidate(args)
    elif args.mark_archived:
        _cmd_mark_archived(args)
    elif args.restore:
        _cmd_restore(args)
    elif args.aggregate:
        _cmd_aggregate(args)
    else:
        _cmd_run(args)


if __name__ == "__main__":
    main()
