"""
Precompute Qmin_env / Qmax_env from NWM data only (WSP geometry + SRC tailwater).

For each dam with Dam_Height_WSP_Ft + Dam_Length_WSP_Ft + a COMID whose SRC
is in synthetic_rating_curves.json:
  - Interpolates tailwater depth from the SRC stage-discharge table
  - Solves for Q where tailwater crosses conjugate (Qmin) and flip (Qmax)
  - Writes results to Qmin_env / Qmax_env (cms)

Also drops ARC-only columns that are no longer used:
  Dam_Height_GIS_Ft, Dam_Length_GIS_Ft, Tailwater_a, Tailwater_b,
  Rp100_cms, Qmin_stable, Qmax_stable

Incremental: dams that already have Qmin_env are skipped, so each
pipeline batch only solves newly-added dams.

Reads and writes the dam table, data/full_lhd_website.parquet (or the table
given with --table). The website reads that same file directly, so no
separate frontend copy is written any more.

Usage
-----
    python backend/compute_wsp_danger_range.py
    python backend/compute_wsp_danger_range.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT    = _BACKEND_ROOT.parent

if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.hydraulics import weir_H_simp, calc_y2_simp, compute_y_flip_adv
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table, write_dam_table

FT_TO_M      = 1.0 / 3.28084
DEFAULT_TABLE = DEFAULT_DAM_TABLE   # data/full_lhd_website.parquet
SRC_JSON     = _REPO_ROOT / "frontend/data" / "synthetic_rating_curves.json"

ARC_COLS = [
    "Dam_Height_GIS_Ft", "Dam_Length_GIS_Ft",
    "Tailwater_a", "Tailwater_b",
    "Rp100_cms",
    "Qmin_stable", "Qmax_stable",
]


def _danger_range_from_src(
    L_m: float, P_m: float, src_qs: list, src_ds: list, n_points: int = 500
) -> tuple[float | None, float | None]:
    """Scan log-spaced Q values identical to the JS buildRatingCurvesFt (500 pts)
    and return (Qmin, Qmax) in cms where yt >= y2 AND yt <= yf."""
    tw = interp1d(src_qs, src_ds, bounds_error=False,
                  fill_value=(src_ds[0], src_ds[-1]))
    q_max = src_qs[-1]
    q_lo  = max(src_qs[0], 1e-6)
    qs = np.exp(np.linspace(np.log(q_lo), np.log(q_max), n_points))

    danger_qs = []
    for Q in qs:
        yt = float(tw(Q))
        H  = weir_H_simp(Q, L_m)
        y2 = calc_y2_simp(H, P_m)
        yf = compute_y_flip_adv(Q, L_m, P_m)
        if y2 == -9999 or yf == -9999:
            continue
        if yt >= y2 and yt <= yf:
            danger_qs.append(Q)

    if not danger_qs:
        return None, None
    return float(min(danger_qs)), float(max(danger_qs))


def run(
    table_path: Path = DEFAULT_TABLE,
    src_data: dict | None = None,
    dry_run: bool = False,
) -> None:
    """Compute NWM-based Qmin_env/Qmax_env and write back to table_path (.parquet).

    Incremental: dams that already have a Qmin_env value are skipped.
    src_data: pre-loaded synthetic_rating_curves.json dict. If None, loads
    from SRC_JSON (avoids re-reading 11 MB per HUC batch when called in a loop).
    """
    df = read_dam_table(table_path)

    if src_data is None:
        with open(SRC_JSON) as f:
            src_data = json.load(f)

    # Drop legacy ARC-only columns (no-op once already removed)
    drop = [c for c in ARC_COLS if c in df.columns]
    if drop:
        df = df.drop(columns=drop)
        print(f"Dropped ARC columns: {drop}")

    # Ensure columns exist without wiping previously-solved values
    if "Qmin_env" not in df.columns:
        df["Qmin_env"] = np.nan
    if "Qmax_env" not in df.columns:
        df["Qmax_env"] = np.nan

    has_wsp = (
        df["Dam_Height_WSP_Ft"].notna() & (df["Dam_Height_WSP_Ft"] > 0) &
        df["Dam_Length_WSP_Ft"].notna() & (df["Dam_Length_WSP_Ft"] > 0)
    )
    has_comid = df["Reach_ID"].notna()
    needs_compute = df["Qmin_env"].isna()
    candidates = df[has_wsp & has_comid & needs_compute].copy()
    candidates["_comid_str"] = (
        candidates["Reach_ID"].astype(float).astype(int).astype(str)
    )
    candidates = candidates[candidates["_comid_str"].isin(src_data)]
    print(f"Candidates (WSP + SRC): {len(candidates):,}")

    ok = failed = 0
    q_mins: dict[int, float] = {}
    q_maxs: dict[int, float] = {}

    for idx, row in candidates.iterrows():
        comid = row["_comid_str"]
        curve = src_data[comid]
        src_qs = curve["discharge_cms"]
        src_ds = curve["stage_m"]
        if len(src_qs) < 2:
            failed += 1
            continue

        P_m = float(row["Dam_Height_WSP_Ft"]) * FT_TO_M
        L_m = float(row["Dam_Length_WSP_Ft"]) * FT_TO_M

        q_min, q_max = _danger_range_from_src(L_m, P_m, src_qs, src_ds)
        if q_min is not None:
            q_mins[idx] = q_min
            q_maxs[idx] = q_max
            ok += 1
        else:
            failed += 1

    print(f"Solved: {ok:,}  |  Failed/degenerate: {failed:,}")

    if dry_run:
        print("Dry run — no files written.")
        return

    df.loc[list(q_mins.keys()), "Qmin_env"] = list(q_mins.values())
    df.loc[list(q_maxs.keys()), "Qmax_env"] = list(q_maxs.values())

    write_dam_table(df, table_path)
    print(f"Wrote {table_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", "--csv", dest="table", type=Path, default=DEFAULT_TABLE,
                        help="Dam table (.parquet) [default: %(default)s]; --csv is an older name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(table_path=args.table, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
