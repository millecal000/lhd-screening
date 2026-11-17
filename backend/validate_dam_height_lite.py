"""
validate_dam_height_lite.py — compare lite estimator and ARC against NID truth.

NID-reported heights (NID_Height_Ft, Hydraulic_Height_Ft, Structural_Height_Ft)
are treated as ground truth.  ARC (Dam_Height_GIS_Ft) is treated as a second
estimator, not truth, so both estimators are evaluated on equal footing.

Output (all in output/validation/ by default):
  dam_height_validation.png  — 2×2 panel: scatter + residuals for each estimator
  dam_height_lite_joined.csv — full joined table for further analysis

Usage
-----
    python backend/validate_dam_height_lite.py
    python backend/validate_dam_height_lite.py \\
        --lite-csv output/dam_height_lite.csv \\
        --dam-table data/full_lhd_website.parquet \\
        --out-dir  output/validation
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LITE = _REPO_ROOT / "output" / "dam_height_lite.csv"
DEFAULT_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
DEFAULT_OUT  = _REPO_ROOT / "output" / "validation"

FT_TO_M = 0.3048

# NID truth column priority — use first non-null in this order
NID_PRIORITY = ["NID_Height_Ft", "Hydraulic_Height_Ft", "Structural_Height_Ft", "Dam_Height_Ft"]


def load_and_join(lite_csv: Path, dam_table: Path) -> pd.DataFrame:
    lite = pd.read_csv(lite_csv)          # output of estimate_dam_height_lite.py (CSV)
    dams = read_dam_table(dam_table)      # master dam table (Parquet)

    dams["OBJECTID"] = pd.to_numeric(dams["OBJECTID"], errors="coerce").astype("Int64")
    lite["OBJECTID"] = pd.to_numeric(lite["OBJECTID"], errors="coerce").astype("Int64")

    # ARC height
    dams["dam_height_ARC_m"] = (
        pd.to_numeric(dams["Dam_Height_GIS_Ft"], errors="coerce") * FT_TO_M
    )

    # NID truth: best available in priority order
    dams["nid_height_m"] = np.nan
    for col in NID_PRIORITY:
        if col not in dams.columns:
            continue
        v = pd.to_numeric(dams[col], errors="coerce") * FT_TO_M
        mask = dams["nid_height_m"].isna() & v.notna() & (v > 0)
        dams.loc[mask, "nid_height_m"] = v[mask]
        dams.loc[mask, "nid_source"] = col

    keep_dam_cols = [
        "OBJECTID", "Dam_Name", "State Abbreviation",
        "dam_height_ARC_m", "nid_height_m", "nid_source",
    ] + [c for c in NID_PRIORITY if c in dams.columns]

    df = lite.merge(dams[keep_dam_cols], on="OBJECTID", how="left")
    return df


def compute_stats(y_pred: np.ndarray, y_true: np.ndarray, label: str) -> dict:
    mask = np.isfinite(y_pred) & np.isfinite(y_true) & (y_true > 0)
    yp, yt = y_pred[mask], y_true[mask]
    if len(yp) < 2:
        return {"label": label, "N": len(yp)}
    resid = yp - yt
    r = float(np.corrcoef(yp, yt)[0, 1])
    return {
        "label":        label,
        "N":            int(len(yp)),
        "RMSE_m":       float(np.sqrt(np.mean(resid ** 2))),
        "MAE_m":        float(np.mean(np.abs(resid))),
        "MBE_m":        float(np.mean(resid)),
        "Pearson_r":    float(r),
        "median_ratio": float(np.median(yp / yt)),
    }


def _scatter_panel(ax, y_true, y_pred, colors, title, stats):
    ax.scatter(y_true, y_pred, c=colors, s=28, alpha=0.7, linewidths=0.3, edgecolors="k")
    lim = max(np.nanmax(y_true), np.nanmax(y_pred)) * 1.05
    ax.plot([0, lim], [0, lim], "k--", lw=1, label="1:1")
    ax.set_xlabel("NID height (m)  [truth]")
    ax.set_ylabel("Estimated height (m)")
    ax.set_title(title)
    txt = (
        f"N={stats.get('N',0)}  RMSE={stats.get('RMSE_m',0):.2f} m\n"
        f"MAE={stats.get('MAE_m',0):.2f} m  bias={stats.get('MBE_m',0):.2f} m\n"
        f"r={stats.get('Pearson_r',0):.3f}  med ratio={stats.get('median_ratio',0):.2f}"
    )
    ax.text(0.03, 0.97, txt, transform=ax.transAxes, va="top", fontsize=8,
            family="monospace", bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8))


def _resid_panel(ax, y_true, resid, colors, title, bias):
    ax.scatter(y_true, resid, c=colors, s=28, alpha=0.7, linewidths=0.3, edgecolors="k")
    ax.axhline(0,    color="k",       lw=1, ls="--")
    ax.axhline(bias, color="#c0392b", lw=1, ls=":",
               label=f"bias = {bias:.2f} m")
    ax.set_xlabel("NID height (m)  [truth]")
    ax.set_ylabel("Residual (est − NID, m)")
    ax.set_title(title)
    ax.legend(fontsize=8)


def plot(df: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    has_nid  = df["nid_height_m"].notna() & (df["nid_height_m"] > 0)
    has_lite = df["dam_height_m"].notna()
    has_arc  = df["dam_height_ARC_m"].notna()

    sub_lite = df[has_nid & has_lite].copy()
    sub_arc  = df[has_nid & has_arc].copy()

    nid_lite = sub_lite["nid_height_m"].values
    est_lite = sub_lite["dam_height_m"].values
    nid_arc  = sub_arc["nid_height_m"].values
    est_arc  = sub_arc["dam_height_ARC_m"].values

    stats_lite = compute_stats(est_lite, nid_lite, "lite")
    stats_arc  = compute_stats(est_arc,  nid_arc,  "ARC")

    # Colour lite points by flag quality
    is_clean = sub_lite["flags"].fillna("").isin(["ok", ""])
    colors_lite = np.where(is_clean, "#2980b9", "#e67e22")
    colors_arc  = "#27ae60"

    fig = plt.figure(figsize=(14, 10))
    gs  = GridSpec(2, 2, figure=fig, hspace=0.45, wspace=0.35)

    # Row 0: scatter
    _scatter_panel(fig.add_subplot(gs[0, 0]),
                   nid_lite, est_lite, colors_lite,
                   "Lite estimator vs NID (truth)", stats_lite)
    _scatter_panel(fig.add_subplot(gs[0, 1]),
                   nid_arc,  est_arc,  colors_arc,
                   "ARC vs NID (truth)", stats_arc)

    # Row 1: residuals
    _resid_panel(fig.add_subplot(gs[1, 0]),
                 nid_lite, est_lite - nid_lite, colors_lite,
                 "Lite residuals vs NID height",
                 stats_lite.get("MBE_m", 0))
    _resid_panel(fig.add_subplot(gs[1, 1]),
                 nid_arc,  est_arc  - nid_arc,  colors_arc,
                 "ARC residuals vs NID height",
                 stats_arc.get("MBE_m", 0))

    # Shared legend for lite point colours
    from matplotlib.lines import Line2D
    fig.legend(
        handles=[
            Line2D([0],[0], marker="o", color="w", markerfacecolor="#2980b9",
                   markersize=8, label="lite — no fallbacks"),
            Line2D([0],[0], marker="o", color="w", markerfacecolor="#e67e22",
                   markersize=8, label="lite — has fallbacks"),
            Line2D([0],[0], marker="o", color="w", markerfacecolor="#27ae60",
                   markersize=8, label="ARC"),
        ],
        loc="lower center", ncol=3, fontsize=9, frameon=True,
        bbox_to_anchor=(0.5, -0.02),
    )

    fig.suptitle(
        "Dam-height validation — NID as ground truth\n"
        f"NID source priority: {' → '.join(NID_PRIORITY)}",
        fontsize=12,
    )

    path = out_dir / "dam_height_validation.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {path}")


def print_nid_source_summary(df: pd.DataFrame) -> None:
    has_nid = df["nid_height_m"].notna()
    print(f"\nNID coverage: {has_nid.sum()}/{len(df)} dams in lite output have NID height")
    if has_nid.any():
        print("NID source breakdown:")
        for src, n in df.loc[has_nid, "nid_source"].value_counts().items():
            print(f"  {n:4d}  {src}")


def print_flag_summary(df: pd.DataFrame) -> None:
    from collections import Counter
    all_flags: list[str] = []
    for s in df["flags"].fillna(""):
        all_flags.extend(f for f in s.split("|") if f)
    counts = Counter(all_flags)
    print("\nLite estimator flag summary:")
    for flag, n in counts.most_common():
        print(f"  {n:4d}  {flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lite-csv", type=Path, default=DEFAULT_LITE)
    # --dam-csv is kept as an old alias of --dam-table.
    ap.add_argument("--dam-table", "--dam-csv", dest="dam_table", type=Path,
                    default=DEFAULT_TABLE)
    ap.add_argument("--out-dir",  type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.lite_csv.exists():
        print(f"ERROR: {args.lite_csv} not found.\nRun estimate_dam_height_lite.py first.")
        return 1

    df    = load_and_join(args.lite_csv, args.dam_table)
    stats = [
        compute_stats(df["dam_height_m"].values,     df["nid_height_m"].values, "lite vs NID"),
        compute_stats(df["dam_height_ARC_m"].values, df["nid_height_m"].values, "ARC  vs NID"),
    ]

    print("\n=== Validation summary (NID as truth) ===")
    for s in stats:
        print(f"\n  [{s['label']}]")
        for k, v in s.items():
            if k == "label":
                continue
            print(f"    {k:<18} {v:.4g}" if isinstance(v, float) else f"    {k:<18} {v}")

    plot(df, args.out_dir)
    print_nid_source_summary(df)
    print_flag_summary(df)

    joined = args.out_dir / "dam_height_lite_joined.csv"
    df.to_csv(joined, index=False)
    print(f"\nJoined table → {joined}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
