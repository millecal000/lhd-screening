"""
WSP pipeline failure diagnostics.

Walks every huc<level>_<KEY>/WSP_RESULTS/<dam_id>/wsp_result.json under
--staging-root, classifies each failure into a human-readable bucket, joins
dam metadata from the master CSV, and writes a summary CSV.

Also reports dams whose HUC batch ran but left no result file (pipeline was
interrupted before they were attempted).

Usage
-----
    python backend/diagnose_wsp_failures.py \\
        --staging-root /data/lhd_wsp \\
        [--dams-table data/full_lhd_website.parquet] \\
        [--output-csv /data/lhd_wsp/wsp_failures.csv] \\
        [--failures-only]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT    = _BACKEND_ROOT.parent
DEFAULT_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet

_HUC_DIR_RE = re.compile(r"^huc\d+_")


# ---------------------------------------------------------------------------
# Failure bucketing
# ---------------------------------------------------------------------------

def _bucket(status: str) -> str:
    if status == "ok":
        return "ok"
    if status == "not_run":
        return "not_run"
    if status == "no_comid":
        return "no_comid"
    if status == "no_flowline":
        return "no_flowline"
    if status == "no_dem":
        return "no_dem"
    if status in ("no_comid_col", "comid_not_in_gpkg"):
        return "comid_missing_from_flowline"
    if status == "insufficient_dem_coverage":
        return "sparse_dem_coverage"
    if status == "negative_delta_wse":
        return "flat_or_inverted_reach"
    if status == "no_tw_bf":
        return "no_channel_width"
    if status == "no_solution":
        return "energy_balance_no_solution"
    if status.startswith("profile_error"):
        return "dem_profile_error"
    if status.startswith("exception"):
        return "unhandled_exception"
    return "other"


_BUCKET_LABELS = {
    "ok":                          "OK",
    "not_run":                     "Not run (pipeline interrupted)",
    "no_comid":                    "No COMID in master CSV",
    "no_flowline":                 "NHDPlus flowline not staged",
    "no_dem":                      "DEM not staged",
    "comid_missing_from_flowline": "COMID missing from flowline gpkg",
    "sparse_dem_coverage":         "Insufficient DEM coverage (<3 pts)",
    "flat_or_inverted_reach":      "Flat or inverted reach (delta_wse ≤ 0)",
    "no_channel_width":            "No bankfull channel width in parquet",
    "energy_balance_no_solution":  "Energy balance: no solution",
    "dem_profile_error":           "DEM profile sampling error",
    "unhandled_exception":         "Unhandled exception",
    "other":                       "Other / unknown",
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_huc_dirs(root: Path) -> List[Path]:
    """Return all huc<N>_<KEY> subdirs that contain a WSP_RESULTS dir."""
    if not root.is_dir():
        sys.exit(f"Staging root not found: {root}")
    dirs = sorted(
        d for d in root.iterdir()
        if d.is_dir() and _HUC_DIR_RE.match(d.name)
        and (d / "WSP_RESULTS").is_dir()
    )
    return dirs


def _load_ledger(root: Path) -> Dict[str, dict]:
    p = root / "wsp_ledger.json"
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def _scan(
    staging_root: Path,
    failures_only: bool,
) -> pd.DataFrame:
    huc_dirs = _discover_huc_dirs(staging_root)
    if not huc_dirs:
        sys.exit(f"No huc*_* dirs with WSP_RESULTS found under {staging_root}")
    print(f"Found {len(huc_dirs)} HUC dir(s) under {staging_root}")

    ledger = _load_ledger(staging_root)

    rows = []
    for huc_dir in huc_dirs:
        key = re.sub(r"^huc\d+_", "", huc_dir.name)
        results_dir = huc_dir / "WSP_RESULTS"

        # All dam IDs the ledger says were in this batch
        ledger_ids = set(ledger.get(key, {}).get("dam_ids", []))

        # All dam IDs that actually have a result file
        result_ids = {
            int(p.parent.name)
            for p in results_dir.rglob("wsp_result.json")
        }

        # Dams the batch was supposed to run but never got a result
        not_run_ids = ledger_ids - result_ids

        # Read every result file
        for p in sorted(results_dir.rglob("wsp_result.json")):
            try:
                dam_id = int(p.parent.name)
            except ValueError:
                continue
            try:
                with open(p) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = {"dam_id": dam_id, "status": "unreadable_json"}

            status = data.get("status", "unknown")
            rows.append({
                "dam_id":       dam_id,
                "huc_key":      key,
                "status":       status,
                "bucket":       _bucket(status),
                "delta_wse_m":  data.get("delta_wse_m"),
                "Q_cms":        data.get("Q_cms"),
                "tw_bf_m":      data.get("tw_bf_m"),
                "y_T_m":        data.get("y_T_m"),
                "P_height_m":   data.get("P_height_m"),
                "crest_length_m": data.get("crest_length_m"),
            })

        # Dams with no result file
        for dam_id in sorted(not_run_ids):
            rows.append({
                "dam_id":  dam_id,
                "huc_key": key,
                "status":  "not_run",
                "bucket":  "not_run",
            })

    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit("No results found — check your staging root path.")

    if failures_only:
        df = df[df["status"] != "ok"].copy()

    return df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-root", type=Path, required=True,
                        help="Parent dir containing huc<N>_<KEY>/ subdirs")
    # --dams-csv is kept as an old alias of --dams-table.
    parser.add_argument("--dams-table", "--dams-csv", dest="dams_table", type=Path,
                        default=DEFAULT_TABLE,
                        help=f"Master dam table (.parquet) for metadata [default: {DEFAULT_TABLE}]")
    parser.add_argument("--output-csv", type=Path, default=None,
                        help="Where to write the per-dam CSV "
                             "[default: <staging-root>/wsp_failures.csv]")
    parser.add_argument("--failures-only", action="store_true",
                        help="Exclude ok dams from the output CSV")
    args = parser.parse_args()

    df = _scan(args.staging_root, args.failures_only)

    # Join master CSV metadata
    if args.dams_table.exists():
        meta_cols = ["OBJECTID", "Dam_Name", "State", "HUC8", "Reach_ID"]
        # Column names come from the Parquet footer, no data is read for this.
        import pyarrow.parquet as _pq
        _hdr = _pq.ParquetFile(args.dams_table).schema_arrow.names
        meta = read_dam_table(args.dams_table,
                              columns=[c for c in meta_cols if c in _hdr])
        meta = meta.rename(columns={"OBJECTID": "dam_id"})
        meta["dam_id"] = pd.to_numeric(meta["dam_id"], errors="coerce").astype("Int64")
        df["dam_id"] = df["dam_id"].astype("Int64")
        df = df.merge(meta, on="dam_id", how="left")
    else:
        print(f"  WARN: dam table not found at {args.dams_table} — skipping metadata join")

    # Column order
    front = ["dam_id", "Dam_Name", "State", "HUC8", "huc_key",
             "status", "bucket", "delta_wse_m", "Q_cms", "tw_bf_m", "y_T_m",
             "P_height_m", "crest_length_m"]
    ordered = [c for c in front if c in df.columns] + \
              [c for c in df.columns if c not in front]
    df = df[ordered]

    # Write CSV
    out = args.output_csv or (args.staging_root / "wsp_failures.csv")
    df.to_csv(out, index=False)
    print(f"\nWrote {len(df):,} rows → {out}")

    # --- Summary ---
    total   = len(df)
    n_ok    = int((df["status"] == "ok").sum())
    n_fail  = total - n_ok

    print(f"\n{'='*55}")
    print(f"  Total dams processed : {total:>6,}")
    print(f"  OK                   : {n_ok:>6,}  ({100*n_ok/total:.1f}%)")
    print(f"  Failed / not run     : {n_fail:>6,}  ({100*n_fail/total:.1f}%)")
    print(f"{'='*55}")

    bucket_counts = (
        df[df["status"] != "ok"]
        .groupby("bucket")
        .size()
        .sort_values(ascending=False)
    )
    if not bucket_counts.empty:
        print(f"\n  Failure breakdown ({n_fail:,} dams):\n")
        for bucket, count in bucket_counts.items():
            label = _BUCKET_LABELS.get(bucket, bucket)
            bar   = "█" * min(30, round(30 * count / n_fail))
            print(f"  {label:<40}  {count:>5,}  {bar}")

    # Per-state breakdown for failures
    if "State" in df.columns:
        state_fail = (
            df[df["status"] != "ok"]
            .groupby("State", dropna=False)
            .size()
            .sort_values(ascending=False)
            .head(15)
        )
        if not state_fail.empty:
            print(f"\n  Top states by failure count:\n")
            for state, count in state_fail.items():
                print(f"    {str(state):<6}  {count:>5,}")

    print()


if __name__ == "__main__":
    main()
