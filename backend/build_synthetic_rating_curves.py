"""
Build synthetic stage-vs-discharge rating curves (SRC) for every dam's NHDPlus V2
reach (Reach_ID / COMID) in data/full_lhd_website.parquet.

Primary method — AHG (at-a-station hydraulic geometry):
    depth  = y_coef * Q^y_exp
    width  = tw_coef * Q^tw_exp
These power-law coefficients are taken directly from the Lynker hydrofabric, which
calibrates them to match NWM Retrospective v3.0 routing.  Because AHG is fit across
the full observed flow range (including out-of-bank events), it implicitly captures
floodplain spreading — the curve naturally flattens at high flows just as NWM does.
Q extends from near-zero up to the NWM p100 (maximum) flow for each reach
(sourced from frontend/data/nwm_fdc.json built by build_nwm_fdc.py).

Fallback — Manning's trapezoid:
Used when AHG coefficients are missing or physically unreasonable (y_exp outside
0.1–0.9).  In-channel only; limited to 2× bankfull stage.

Source: s3://lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet

Usage:
    python backend/build_synthetic_rating_curves.py
    python backend/build_synthetic_rating_curves.py --table data/full_lhd_website.parquet \
        --out frontend/data/synthetic_rating_curves.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table
import pyarrow.parquet as pq
import s3fs

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent

_PARQUET_URL = "lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet"
_CACHE_PATH = _BACKEND_ROOT / "cache" / "riverml_channel_geometry_with_ahg.parquet"

DEFAULT_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
DEFAULT_OUT = _REPO_ROOT / "frontend" / "data" / "synthetic_rating_curves.json"

_N_CURVE_POINTS = 60
_AHG_Y_EXP_MIN, _AHG_Y_EXP_MAX = 0.1, 0.9   # reject outliers outside Leopold & Maddock range
_Q_MAX_BUFFER = 1.0                            # cap at FDC p100
_Q_MAX_FALLBACK_MULT = 20.0                    # × Q_bf when FDC unavailable

# Manning's trapezoid fallback
_N_STAGE_POINTS = 40
_STAGE_MAX_MULTIPLIER = 2.0
_MIN_SLOPE = 1e-4
_MIN_BOTTOM_WIDTH_FRAC = 0.05

DEFAULT_FDC = _REPO_ROOT / "frontend" / "data" / "nwm_fdc.json"


def _log(msg: str) -> None:
    print(f"[build_src] {msg}", flush=True)


def _download_geometry_table(force: bool) -> Path:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _CACHE_PATH.exists() and not force:
        _log(f"Using cached {_CACHE_PATH}")
        return _CACHE_PATH
    _log(f"Downloading s3://{_PARQUET_URL} (anonymous, ~188MB, one-time) ...")
    fs = s3fs.S3FileSystem(anon=True)
    fs.get(_PARQUET_URL, str(_CACHE_PATH))
    _log(f"Cached to {_CACHE_PATH}")
    return _CACHE_PATH


def _load_channel_geometry(parquet_path: Path, feature_ids: set[int]) -> pd.DataFrame:
    table = pq.read_table(
        parquet_path,
        columns=[
            "feature_id",
            "owp_y_bf", "owp_tw_bf", "bf_area",
            "owp_roughness_bathy", "owp_roughness_no_bathy", "slope",
            "y_coef", "y_exp", "tw_coef", "tw_exp",
        ],
        filters=[("feature_id", "in", feature_ids)],
    )
    return table.to_pandas()


def _ahg_rating_curve(row: pd.Series, q_max_cms: float | None = None) -> dict | None:
    """AHG-based SRC: depth = y_coef * Q^y_exp, calibrated to NWM Retrospective v3.0."""
    y_coef  = float(row.get("y_coef",  np.nan))
    y_exp   = float(row.get("y_exp",   np.nan))
    tw_coef = float(row.get("tw_coef", np.nan))
    tw_exp  = float(row.get("tw_exp",  np.nan))
    y_bf    = float(row.get("owp_y_bf", np.nan))

    if not (np.isfinite(y_coef) and y_coef > 0 and
            np.isfinite(y_exp) and _AHG_Y_EXP_MIN <= y_exp <= _AHG_Y_EXP_MAX):
        return None

    # Bankfull discharge from AHG inversion: y_bf = y_coef * Q_bf^y_exp
    Q_bf = ((y_bf / y_coef) ** (1.0 / y_exp)
            if np.isfinite(y_bf) and y_bf > 0 else 10.0)

    q_upper = (q_max_cms * _Q_MAX_BUFFER if q_max_cms else Q_bf * _Q_MAX_FALLBACK_MULT)
    q_upper = max(q_upper, 1.0)

    Q     = np.logspace(np.log10(max(Q_bf * 1e-4, 1e-3)), np.log10(q_upper), _N_CURVE_POINTS)
    stage = y_coef * Q ** y_exp

    tw_bf = float(row.get("owp_tw_bf", np.nan))
    return {
        "stage_m":       [round(float(s), 4) for s in stage],
        "discharge_cms": [round(float(q), 4) for q in Q],
        "bankfull_stage_m": round(float(y_coef * Q_bf ** y_exp), 4),
        "bankfull_tw_m":    round(float(tw_bf), 4) if np.isfinite(tw_bf) else None,
        "y_coef": round(y_coef, 6),
        "y_exp":  round(y_exp,  6),
        "method": "ahg",
    }


def _trapezoid_rating_curve(row: pd.Series) -> dict | None:
    y_bf = float(row["owp_y_bf"])
    tw_bf = float(row["owp_tw_bf"])
    area_bf = float(row["bf_area"])
    slope = float(row["slope"])
    n = row["owp_roughness_bathy"]
    if pd.isna(n) or n <= 0:
        n = row["owp_roughness_no_bathy"]

    if not all(np.isfinite(v) and v > 0 for v in (y_bf, tw_bf, area_bf, n)):
        return None

    slope = max(slope, _MIN_SLOPE) if np.isfinite(slope) else _MIN_SLOPE

    # Back out an equivalent trapezoid from bankfull depth/top-width/area:
    #   area_bf = bw*y_bf + z*y_bf^2  and  tw_bf = bw + 2*z*y_bf
    # => z = (tw_bf - area_bf/y_bf) / y_bf
    side_slope = (tw_bf - area_bf / y_bf) / y_bf
    side_slope = max(side_slope, 0.0)
    bottom_width = tw_bf - 2 * side_slope * y_bf
    bottom_width = max(bottom_width, _MIN_BOTTOM_WIDTH_FRAC * tw_bf)

    stages = np.linspace(0.0, y_bf * _STAGE_MAX_MULTIPLIER, _N_STAGE_POINTS)
    stages[0] = 1e-3  # avoid a literal zero-flow point
    width = bottom_width + 2 * side_slope * stages
    area = stages * bottom_width + side_slope * stages**2
    perimeter = bottom_width + 2 * stages * np.sqrt(1 + side_slope**2)
    hydraulic_radius = area / perimeter
    discharge = (1.0 / float(n)) * area * hydraulic_radius ** (2.0 / 3.0) * slope**0.5

    return {
        "stage_m": [round(float(s), 4) for s in stages],
        "discharge_cms": [round(float(q), 4) for q in discharge],
        "bankfull_stage_m": round(y_bf, 4),
        "bottom_width_m": round(bottom_width, 4),
        "side_slope": round(side_slope, 4),
        "manning_n": round(float(n), 4),
        "slope": round(slope, 6),
        "method": "trapezoid",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --csv is kept as an old alias of --table so existing commands still parse.
    ap.add_argument("--table", "--csv", dest="table", type=Path, default=DEFAULT_TABLE,
                    help="Master dam table (.parquet) [default: data/full_lhd_website.parquet]")
    ap.add_argument("--out",          type=Path, default=DEFAULT_OUT)
    ap.add_argument("--split-dir",    type=Path,
                    default=DEFAULT_OUT.parent / "src",
                    help="Also write one JSON file per ComID here (default: frontend/data/src/)")
    ap.add_argument("--fdc",          type=Path, default=DEFAULT_FDC,
                    help="nwm_fdc.json from build_nwm_fdc.py (provides Q_max per reach)")
    ap.add_argument("--force-download", action="store_true")
    args = ap.parse_args()

    if not args.table.exists():
        _log(f"ERROR: {args.table} not found"); return 1

    # Load FDC for Q_max per ComID (p100 = index 12 in the 13-value array)
    fdc: dict = {}
    if args.fdc.exists():
        with open(args.fdc) as f:
            raw = json.load(f)
        raw.pop("_meta", None)
        # p100 is the last value (index 12) — maximum NWM flow
        fdc = {k: v[12] for k, v in raw.items() if isinstance(v, list) and len(v) == 13}
        _log(f"Loaded Q_max for {len(fdc):,} reaches from {args.fdc.name}")
    else:
        _log(f"WARNING: {args.fdc} not found — Q_max will use 20×Q_bf fallback")

    _log(f"Loading {args.table} ...")
    df = read_dam_table(args.table)
    reach_ids   = pd.to_numeric(df["Reach_ID"], errors="coerce").dropna().astype(int)
    feature_ids = set(reach_ids.unique().tolist())
    _log(f"{len(feature_ids)} unique Reach_ID/COMID values across {len(df)} dams")

    parquet_path = _download_geometry_table(args.force_download)

    _log("Filtering channel geometry table to dam reaches ...")
    geom = _load_channel_geometry(parquet_path, feature_ids)
    _log(f"Matched {len(geom)}/{len(feature_ids)} reaches in hydrofabric")

    curves: dict[str, dict] = {}
    n_ahg = n_trap = n_fail = 0
    for _, row in geom.iterrows():
        comid_str = str(int(row["feature_id"]))
        q_max = fdc.get(comid_str)
        curve = _ahg_rating_curve(row, q_max_cms=q_max)
        if curve is not None:
            n_ahg += 1
        else:
            curve = _trapezoid_rating_curve(row)
            if curve is not None:
                n_trap += 1
            else:
                n_fail += 1
        if curve is not None:
            curves[comid_str] = curve

    _log(f"Curves: {n_ahg} AHG, {n_trap} trapezoid fallback, {n_fail} failed")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(curves, f, separators=(",", ":"))
    _log(f"Wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")

    if args.split_dir:
        args.split_dir.mkdir(parents=True, exist_ok=True)
        for comid, curve in curves.items():
            with open(args.split_dir / f"{comid}.json", "w") as f:
                json.dump(curve, f, separators=(",", ":"))
        _log(f"Wrote {len(curves)} per-COMID files to {args.split_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
