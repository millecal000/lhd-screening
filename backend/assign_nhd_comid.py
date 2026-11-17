"""
Assign NHDPlus V2 COMID + GNIS river name to every dam in full_lhd_website.parquet.

The NHDPlus V2 COMID is the feature_id used by the National Water Model, so once
this column is populated for every dam we can swap forecast lookups from
GEOGLOWS to NWM without per-dam re-keying.

Phase A
    For each dam with valid lat/lon, query USGS NLDI `comid_byloc` to get the
    nearest NHDFlowline COMID. Runs in parallel with retries and writes
    progress to a JSON checkpoint after every successful lookup so the run is
    resumable.

Phase B
    Load the cached NHDPlus VAA table once and look up `gnis_name` for each
    COMID locally (no further web calls).

Output
    Overwrites `Reach_ID` and adds `GNIS_Name` in
    data/full_lhd_website.parquet.

Usage
    python backend/assign_nhd_comid.py
    python backend/assign_nhd_comid.py --workers 24 --limit 100
    python backend/assign_nhd_comid.py --force      # ignore checkpoint, re-fetch all
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd

# Master dam table (Parquet) reader/writer shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table, write_dam_table
import pynhd as nhd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NLDI_URL = "https://labs-beta.waterdata.usgs.gov/api/nldi/linked-data/comid/position"

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
DEFAULT_CHECKPOINT = _BACKEND_ROOT / "data" / "nhd_comid_checkpoint.json"

_print_lock = threading.Lock()
_ckpt_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _load_checkpoint(path: Path) -> dict[str, Optional[int]]:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return {str(k): v for k, v in json.load(f).items()}
    except Exception as e:
        _log(f"Warning: could not parse checkpoint ({e}) — starting fresh")
        return {}


def _save_checkpoint(path: Path, data: dict[str, Optional[int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(path)


def _make_session() -> requests.Session:
    """HTTP session with retries on 5xx + connection errors. 404 is NOT retried."""
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32))
    return s


def _lookup_comid(session: requests.Session, lon: float, lat: float) -> Optional[int]:
    """Return the NHDPlus V2 COMID for (lon, lat).

    - HTTP 404 means NLDI has no flowline at this point (closed basin, outside
      CONUS, off-network) — return None immediately, no retry.
    - Real network/5xx errors are retried by the session adapter.
    """
    try:
        r = session.get(
            NLDI_URL,
            params={"coords": f"POINT({lon} {lat})"},
            timeout=30,
        )
    except requests.RequestException as e:
        _log(f"  NLDI net fail @ ({lat:.4f},{lon:.4f}): {e}")
        return None

    if r.status_code == 404:
        return None
    if r.status_code != 200:
        _log(f"  NLDI HTTP {r.status_code} @ ({lat:.4f},{lon:.4f})")
        return None

    try:
        payload = r.json()
    except ValueError:
        return None

    features = payload.get("features") or []
    if not features:
        return None
    props = features[0].get("properties") or {}
    comid = props.get("comid") or props.get("identifier")
    if comid is None:
        return None
    try:
        return int(comid)
    except (TypeError, ValueError):
        return None


def phase_a_fetch_comids(
    df: pd.DataFrame,
    checkpoint_path: Path,
    workers: int,
    flush_every: int,
    force: bool,
) -> dict[str, Optional[int]]:
    """Populate {objectid -> comid} via parallel NLDI lookups."""
    checkpoint = {} if force else _load_checkpoint(checkpoint_path)
    session = _make_session()

    todo: list[tuple[str, float, float]] = []
    for _, row in df.iterrows():
        oid = str(int(row["OBJECTID"]))
        if oid in checkpoint:
            continue
        try:
            lat = float(row["Latitude"])
            lon = float(row["Longitude"])
        except (TypeError, ValueError):
            checkpoint[oid] = None
            continue
        if pd.isna(lat) or pd.isna(lon):
            checkpoint[oid] = None
            continue
        todo.append((oid, lon, lat))

    _log(f"Phase A: {len(checkpoint)} cached, {len(todo)} to fetch with {workers} workers")
    if not todo:
        return checkpoint

    done_count = 0
    last_flush = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_lookup_comid, session, lon, lat): oid
            for oid, lon, lat in todo
        }
        _log(f"  submitted {len(futures)} requests, waiting for results ...")
        for fut in as_completed(futures):
            oid = futures[fut]
            try:
                comid = fut.result()
            except Exception as e:
                _log(f"  unexpected error for {oid}: {e}")
                comid = None
            with _ckpt_lock:
                checkpoint[oid] = comid
                done_count += 1
                if done_count - last_flush >= flush_every:
                    _save_checkpoint(checkpoint_path, checkpoint)
                    last_flush = done_count
                    rate = done_count / max(time.time() - start, 1e-6)
                    remaining = (len(todo) - done_count) / max(rate, 1e-6)
                    _log(
                        f"  {done_count}/{len(todo)} done  "
                        f"({rate:.1f}/s, ~{remaining/60:.1f} min left)"
                    )

    _save_checkpoint(checkpoint_path, checkpoint)
    hits = sum(1 for v in checkpoint.values() if v is not None)
    _log(f"Phase A complete: {hits}/{len(checkpoint)} got a COMID")
    return checkpoint


def phase_b_lookup_gnis(comids: list[int]) -> dict[int, Optional[str]]:
    """Map each COMID to its gnis_name from the cached NHDPlus VAA table."""
    _log("Phase B: loading NHDPlus VAA (cached after first run) ...")
    vaa = nhd.nhdplus_vaa()
    if "gnis_name" not in vaa.columns:
        _log(f"  Unexpected: VAA missing gnis_name. Columns: {list(vaa.columns)[:20]}")
        return {c: None for c in comids}

    subset = vaa[["comid", "gnis_name"]].dropna(subset=["comid"]).copy()
    subset["comid"] = pd.to_numeric(subset["comid"], errors="coerce").astype("Int64")
    name_map = dict(zip(subset["comid"].dropna().astype(int), subset["gnis_name"]))

    out: dict[int, Optional[str]] = {}
    for c in comids:
        name = name_map.get(c)
        if isinstance(name, str):
            name = name.strip()
            out[c] = name or None
        else:
            out[c] = None
    hits = sum(1 for v in out.values() if v)
    _log(f"Phase B complete: {hits}/{len(out)} COMIDs have a GNIS name")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --csv is kept as an old alias of --table.
    ap.add_argument("--table", "--csv", dest="table", type=Path, default=DEFAULT_TABLE,
                    help="dam table (.parquet) to update in place")
    ap.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--flush-every", type=int, default=25, help="save checkpoint + log progress every N lookups")
    ap.add_argument("--limit", type=int, default=None, help="only process first N dams (smoke test)")
    ap.add_argument("--force", action="store_true", help="ignore checkpoint, re-fetch everything")
    ap.add_argument("--phase-a-only", action="store_true")
    ap.add_argument("--phase-b-only", action="store_true", help="skip NLDI; just rebuild GNIS from checkpoint")
    args = ap.parse_args()

    if not args.table.exists():
        _log(f"ERROR: {args.table} not found")
        return 1

    _log(f"Loading {args.table} ...")
    df = read_dam_table(args.table)
    _log(f"  {len(df)} rows")

    work_df = df.head(args.limit) if args.limit else df

    if not args.phase_b_only:
        checkpoint = phase_a_fetch_comids(
            work_df,
            args.checkpoint,
            workers=args.workers,
            flush_every=args.flush_every,
            force=args.force,
        )
    else:
        checkpoint = _load_checkpoint(args.checkpoint)
        _log(f"Phase B only: loaded {len(checkpoint)} cached comids")

    if args.phase_a_only:
        _log("Phase A only — done.")
        return 0

    unique_comids = sorted({v for v in checkpoint.values() if v is not None})
    gnis = phase_b_lookup_gnis(unique_comids)

    if "GNIS_Name" not in df.columns:
        df["GNIS_Name"] = pd.NA

    oid_str = df["OBJECTID"].astype(int).astype(str)
    new_comid = oid_str.map(checkpoint)
    new_gnis = new_comid.map(lambda c: gnis.get(int(c)) if pd.notna(c) else pd.NA)

    touched = oid_str.isin(checkpoint.keys())
    n_touched = int(touched.sum())
    n_untouched = int((~touched).sum())
    _log(f"Merging: {n_touched} rows updated, {n_untouched} rows left untouched (no checkpoint entry)")

    df.loc[touched, "Reach_ID"] = new_comid[touched]
    df.loc[touched, "GNIS_Name"] = new_gnis[touched]
    df["Reach_ID"] = pd.to_numeric(df["Reach_ID"], errors="coerce").astype("Int64")

    _log(f"Writing {args.table} ...")
    # Reach_ID is Int64 here; write_dam_table stores it as the CSV did
    # (int64 with no gaps, float64 with gaps).
    write_dam_table(df, args.table)
    _log(
        f"Done. Reach_ID filled: {df['Reach_ID'].notna().sum()}/{len(df)}  "
        f"GNIS_Name filled: {df['GNIS_Name'].notna().sum()}/{len(df)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
