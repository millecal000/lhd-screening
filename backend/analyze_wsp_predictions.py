"""
Compare WSP-derived dam height and length against NID reference values.

Usage:
    python backend/analyze_wsp_predictions.py
    python backend/analyze_wsp_predictions.py --table data/full_lhd_website.parquet \
        --out output/wsp_validation.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table
from scipy import stats

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
DEFAULT_OUT = _REPO_ROOT / "output" / "wsp_validation.png"


def metrics(pred: np.ndarray, obs: np.ndarray) -> dict:
    residuals = pred - obs
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((obs - obs.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    rmse = np.sqrt(np.mean(residuals ** 2))
    mae = np.mean(np.abs(residuals))
    bias = np.mean(residuals)
    slope, intercept, r, *_ = stats.linregress(obs, pred)
    return dict(n=len(pred), r2=r2, rmse=rmse, mae=mae, bias=bias,
                slope=slope, intercept=intercept, r=r)


def scatter_panel(ax, obs, pred, label_obs, label_pred, unit, color, cap=None):
    if cap:
        mask = (obs <= cap) & (pred <= cap)
        obs, pred = obs[mask], pred[mask]

    m = metrics(pred.values, obs.values)

    ax.scatter(obs, pred, alpha=0.25, s=8, color=color, linewidths=0)

    lim = max(obs.max(), pred.max()) * 1.05
    ax.plot([0, lim], [0, lim], "k--", lw=0.8, label="1:1")
    x_fit = np.array([0, lim])
    ax.plot(x_fit, m["slope"] * x_fit + m["intercept"],
            color=color, lw=1.2, label=f"fit (slope={m['slope']:.2f})")

    stats_text = (
        f"n={m['n']:,}\n"
        f"R²={m['r2']:.3f}\n"
        f"RMSE={m['rmse']:.1f} {unit}\n"
        f"MAE={m['mae']:.1f} {unit}\n"
        f"Bias={m['bias']:+.1f} {unit}"
    )
    ax.text(0.97, 0.03, stats_text, transform=ax.transAxes,
            ha="right", va="bottom", fontsize=7.5,
            bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8))

    ax.set_xlabel(f"{label_obs} ({unit})")
    ax.set_ylabel(f"{label_pred} ({unit})")
    ax.legend(fontsize=7.5)
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.set_aspect("equal")
    return m


def residual_panel(ax, obs, pred, unit, color, cap=None):
    if cap:
        mask = (obs <= cap) & (pred <= cap)
        obs, pred = obs[mask], pred[mask]
    residuals = pred - obs
    ax.axhline(0, color="k", lw=0.8, ls="--")
    ax.scatter(obs, residuals, alpha=0.25, s=8, color=color, linewidths=0)
    ax.set_xlabel(f"NID observed ({unit})")
    ax.set_ylabel(f"WSP − NID ({unit})")
    # shade ±50% band
    ax.fill_between([0, obs.max() * 1.05],
                    [-obs.max() * 0.5, -obs.max() * 0.5],
                    [obs.max() * 0.5,  obs.max() * 0.5],
                    alpha=0.07, color=color, label="±50%")
    ax.legend(fontsize=7.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # --csv is kept as an old alias of --table so existing commands still parse.
    parser.add_argument("--table", "--csv", dest="table", type=Path, default=DEFAULT_TABLE,
                        help="Master dam table (.parquet) [default: data/full_lhd_website.parquet]")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--height-cap", type=float, default=100,
                        help="Cap NID height at this value for zoomed panels (ft)")
    parser.add_argument("--length-cap", type=float, default=500,
                        help="Cap NID length at this value for zoomed panels (ft)")
    args = parser.parse_args()

    df = read_dam_table(args.table)

    # Height: use NID_Height_Ft (most paired rows)
    h = df[["NID_Height_Ft", "Dam_Height_WSP_Ft"]].dropna()
    h = h[(h["NID_Height_Ft"] > 0) & (h["Dam_Height_WSP_Ft"] >= 0)]

    # Length: use Dam_Length_Ft
    l = df[["Dam_Length_Ft", "Dam_Length_WSP_Ft"]].dropna()
    l = l[(l["Dam_Length_Ft"] > 0) & (l["Dam_Length_WSP_Ft"] > 0)]

    fig, axes = plt.subplots(2, 3, figsize=(14, 9))
    fig.suptitle("WSP-derived vs NID: Height and Length validation", fontsize=12, y=0.98)

    blue = "#2166ac"
    orange = "#d6604d"

    # Row 1 — Height
    scatter_panel(axes[0, 0], h["NID_Height_Ft"], h["Dam_Height_WSP_Ft"],
                  "NID Height", "WSP Height", "ft", blue)
    axes[0, 0].set_title("Height — all data")

    scatter_panel(axes[0, 1], h["NID_Height_Ft"], h["Dam_Height_WSP_Ft"],
                  "NID Height", "WSP Height", "ft", blue, cap=args.height_cap)
    axes[0, 1].set_title(f"Height — NID ≤ {args.height_cap:.0f} ft")

    residual_panel(axes[0, 2], h["NID_Height_Ft"], h["Dam_Height_WSP_Ft"],
                   "ft", blue, cap=args.height_cap)
    axes[0, 2].set_title(f"Height residuals (NID ≤ {args.height_cap:.0f} ft)")

    # Row 2 — Length
    scatter_panel(axes[1, 0], l["Dam_Length_Ft"], l["Dam_Length_WSP_Ft"],
                  "NID Length", "WSP Length", "ft", orange)
    axes[1, 0].set_title("Length — all data")

    scatter_panel(axes[1, 1], l["Dam_Length_Ft"], l["Dam_Length_WSP_Ft"],
                  "NID Length", "WSP Length", "ft", orange, cap=args.length_cap)
    axes[1, 1].set_title(f"Length — NID ≤ {args.length_cap:.0f} ft")

    residual_panel(axes[1, 2], l["Dam_Length_Ft"], l["Dam_Length_WSP_Ft"],
                   "ft", orange, cap=args.length_cap)
    axes[1, 2].set_title(f"Length residuals (NID ≤ {args.length_cap:.0f} ft)")

    for ax in axes.flat:
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    plt.tight_layout()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=150, bbox_inches="tight")
    print(f"Saved → {args.out}")

    # Print summary table
    h_all = metrics(h["Dam_Height_WSP_Ft"].values, h["NID_Height_Ft"].values)
    h_cap = metrics(*[v.values for v in [
        h.loc[h["NID_Height_Ft"] <= args.height_cap, "Dam_Height_WSP_Ft"],
        h.loc[h["NID_Height_Ft"] <= args.height_cap, "NID_Height_Ft"],
    ]])
    l_all = metrics(l["Dam_Length_WSP_Ft"].values, l["Dam_Length_Ft"].values)
    l_cap = metrics(*[v.values for v in [
        l.loc[l["Dam_Length_Ft"] <= args.length_cap, "Dam_Length_WSP_Ft"],
        l.loc[l["Dam_Length_Ft"] <= args.length_cap, "Dam_Length_Ft"],
    ]])

    print(f"\n{'':30} {'n':>6} {'R²':>7} {'RMSE':>8} {'MAE':>7} {'Bias':>8} {'Slope':>7}")
    print("-" * 80)
    for label, m, unit in [
        (f"Height — all (ft)",             h_all, "ft"),
        (f"Height — NID≤{args.height_cap:.0f}ft",  h_cap, "ft"),
        (f"Length — all (ft)",             l_all, "ft"),
        (f"Length — NID≤{args.length_cap:.0f}ft", l_cap, "ft"),
    ]:
        print(f"{label:30} {m['n']:>6,} {m['r2']:>7.3f} {m['rmse']:>7.1f}{unit} "
              f"{m['mae']:>6.1f}{unit} {m['bias']:>+7.1f}{unit} {m['slope']:>7.2f}")


if __name__ == "__main__":
    main()
