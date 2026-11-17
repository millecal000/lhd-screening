"""
Per-dam failure diagnostics for the LHD screening pipeline.

For every dam listed in any tile_manifest.json under --staging-dir, walks
the staged inputs, ARC outputs, analysis summary, and failures_*.json
files to classify exactly *where* the pipeline stopped and *why*.
Read-only — does not mutate any pipeline artifact.

Layout-aware: --staging-dir may be either a flat staging dir (with a
tile_manifest.json at its root) or a parent root containing
huc<level>_<KEY>/ subdirs that each hold one. Diagnostics are
concatenated across all discovered subdirs into a single CSV.

Usage
-----
    python backend/diagnose_failures.py --staging-dir /data/lhd_staging \
        [--dams-table data/full_lhd_website.parquet] \
        [--output-csv /data/lhd_staging/diagnostics.csv]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet

_HUC_DIR_RE = re.compile(r"^huc\d+_")


# ---------------------------------------------------------------------------
# Staging discovery (mirrors run_analysis_batch.py)
# ---------------------------------------------------------------------------

def _discover_staging_dirs(root: Path) -> List[Path]:
    if (root / "tile_manifest.json").exists():
        return [root]
    if not root.is_dir():
        return []
    return sorted(
        d for d in root.iterdir()
        if d.is_dir()
        and _HUC_DIR_RE.match(d.name)
        and (d / "tile_manifest.json").exists()
    )


# ---------------------------------------------------------------------------
# Drilldown buckets for ARC and analysis failure status strings
# ---------------------------------------------------------------------------

def _bucket_arc_reason(status: str) -> str:
    s = status.lower()
    if status.startswith("arc-no-"):
        return "missing_output"
    if status.startswith("missing-input"):
        return "missing_input"
    if "baseflow" in s or "q_ep_50" in s or "q_min" in s or "qmin" in s:
        return "no_baseflow"
    if "rp100" in s or "q_max" in s or "qmax" in s:
        return "no_qmax"
    if "curve" in s or "rating" in s:
        return "rating_curve"
    if "oserror" in s or "permission" in s or "no space" in s or "disk" in s:
        return "io"
    if status.startswith("init-error"):
        return "init"
    if status.startswith("arc-error"):
        return "arc_runtime"
    return "other"


def _bucket_analysis_reason(status: str) -> str:
    if status == "missing-arc":
        return "arc_not_done_first"
    if status == "missing-input":
        return "missing_input"
    if status == "no-baseflow":
        return "no_baseflow"
    if status.startswith("weir-error"):
        return "weir_length"
    if status.startswith("localxs-error"):
        return "local_xs"
    if status.startswith("init-error"):
        return "init"
    if status.startswith("analysis-error"):
        s = status.lower()
        if "rating" in s or "curve" in s:
            return "rating_curve"
        if "height" in s or "p_height" in s:
            return "dam_height"
        return "analysis_runtime"
    if status.startswith("arc-no-"):
        return "missing_arc_output"
    return "other"


# ---------------------------------------------------------------------------
# Per-dam classification
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _staged_paths(staging_dir: Path, dam_id: int) -> Dict[str, Path]:
    """Per-dam file paths we expect to see at each pipeline stage."""
    strm_site = staging_dir / "STRM" / str(dam_id)
    gpkgs = list(strm_site.glob("nhd_flowline_*.gpkg")) if strm_site.exists() else []
    return {
        "strm_dir": strm_site,
        "flowline_gpkg": gpkgs[0] if gpkgs else None,
        "dem": staging_dir / "DEM" / str(dam_id) / f"dem_{dam_id}.tif",
        "land": staging_dir / "LAND" / str(dam_id) / "constant_land.tif",
        "manning": staging_dir / "LAND" / str(dam_id) / "Manning_n.txt",
        "flow_csv": staging_dir / "FLOW" / str(dam_id) / f"flow_{dam_id}.csv",
        "arc_done": staging_dir / "RESULTS" / str(dam_id) / "arc_done.json",
        "analysis_summary": staging_dir / "RESULTS" / str(dam_id) / "analysis_summary.json",
    }


def _is_sentinel(v) -> bool:
    """True if v is one of ARC's no-data sentinels or a NaN."""
    if v is None:
        return True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    return f != f or f == -9999.0  # NaN check via self-inequality


def _summary_facts(summary: dict) -> Dict[str, object]:
    """Pull every fact diagnostics needs out of analysis_summary.json in one pass."""
    xs_list = summary.get("xs_results") or []
    valid = [
        xs for xs in xs_list
        if xs.get("Qmin_cms") is not None and xs.get("Qmax_cms") is not None
    ]
    any_xs_sentinel = any(
        _is_sentinel(xs.get("Qmin_cms")) or _is_sentinel(xs.get("Qmax_cms"))
        or _is_sentinel(xs.get("P_height_m"))
        for xs in xs_list
    )
    qmin_env = min((float(xs["Qmin_cms"]) for xs in valid), default=None)
    qmax_env = max((float(xs["Qmax_cms"]) for xs in valid), default=None)
    bracket = summary.get("solver_bracket_cms") or [None, None]
    return {
        "qmin_env": qmin_env,
        "qmax_env": qmax_env,
        "bracket_source": summary.get("solver_bracket_source"),
        "bracket_floor": bracket[0] if len(bracket) >= 1 else None,
        "bracket_ceiling": bracket[1] if len(bracket) >= 2 else None,
        "dam_height_m": summary.get("dam_height_m"),
        "any_xs_sentinel": any_xs_sentinel,
        "n_xs_total": len(xs_list),
        "n_xs_valid": len(valid),
    }


def _close(a: Optional[float], b: Optional[float], rtol: float = 1e-9, atol: float = 1e-12) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= atol + rtol * max(abs(a), abs(b))


def _flow_values(flow_csv: Path, comid: Optional[int]) -> Tuple[Optional[float], Optional[float]]:
    if not flow_csv.exists():
        return None, None
    try:
        df = pd.read_csv(flow_csv)
    except Exception:
        return None, None
    if df.empty:
        return None, None

    def pick(col: str) -> Optional[float]:
        if col not in df.columns:
            return None
        if comid is not None and "comid" in df.columns:
            match = df.loc[df["comid"] == int(comid), col]
            if not match.empty and pd.notna(match.iloc[0]):
                return float(match.iloc[0])
        valid = df[col].dropna()
        return float(valid.iloc[0]) if not valid.empty else None

    return pick("q_ep_50"), pick("rp100")


def _classify_dam(
    dam_id: int,
    comid: Optional[int],
    staging_dir: Path,
    arc_failures: Dict[int, str],
    analysis_failures: Dict[int, str],
) -> Dict[str, object]:
    paths = _staged_paths(staging_dir, dam_id)
    has_strm = paths["flowline_gpkg"] is not None
    has_dem = paths["dem"].exists()
    has_land = paths["land"].exists() and paths["manning"].exists()
    has_flow_csv = paths["flow_csv"].exists()
    has_arc = paths["arc_done"].exists()
    has_analysis = paths["analysis_summary"].exists()

    q_ep_50, rp100 = _flow_values(paths["flow_csv"], comid)

    facts: Optional[Dict[str, object]] = None
    summary_unreadable = False
    if has_analysis:
        summary = _read_json(paths["analysis_summary"])
        if summary is None:
            summary_unreadable = True
        else:
            facts = _summary_facts(summary)

    row: Dict[str, object] = {
        "OBJECTID": dam_id,
        "huc_dir": staging_dir.name,
        "has_strm": has_strm,
        "has_dem": has_dem,
        "has_land": has_land,
        "has_flow": has_flow_csv,
        "has_arc": has_arc,
        "has_analysis": has_analysis,
        "q_ep_50": q_ep_50,
        "rp100": rp100,
        "qmin_env": facts["qmin_env"] if facts else None,
        "qmax_env": facts["qmax_env"] if facts else None,
        "dam_height_m": facts["dam_height_m"] if facts else None,
        "solver_bracket_source": facts["bracket_source"] if facts else None,
        "terminal_stage": "",
        "terminal_reason": "",
        "raw_reason": "",
    }

    # ---- Staging-stage classification (first match wins) ----
    if not has_strm:
        row["terminal_stage"] = "stage_no_strm"
        row["terminal_reason"] = "no nhd_flowline_*.gpkg under STRM/<id>/"
        return row
    if not has_dem:
        row["terminal_stage"] = "stage_no_dem"
        row["terminal_reason"] = f"missing {paths['dem'].name}"
        return row
    if not has_land:
        missing = []
        if not paths["land"].exists():
            missing.append("constant_land.tif")
        if not paths["manning"].exists():
            missing.append("Manning_n.txt")
        row["terminal_stage"] = "stage_no_land"
        row["terminal_reason"] = f"missing {' + '.join(missing)} under LAND/<id>/"
        return row
    if not has_flow_csv:
        row["terminal_stage"] = "stage_no_flow"
        row["terminal_reason"] = f"missing {paths['flow_csv'].name}"
        return row
    if q_ep_50 is None or q_ep_50 <= 0:
        row["terminal_stage"] = "stage_flow_zero_baseflow"
        row["terminal_reason"] = (
            "q_ep_50 missing/zero — canal or off-network reach"
            if q_ep_50 is not None else "q_ep_50 column missing for this comid"
        )
        return row

    # ---- ARC stage ----
    if not has_arc:
        arc_status = arc_failures.get(dam_id)
        if arc_status:
            row["terminal_stage"] = "arc_failed"
            row["terminal_reason"] = _bucket_arc_reason(arc_status)
            row["raw_reason"] = arc_status
        else:
            row["terminal_stage"] = "arc_not_run"
            row["terminal_reason"] = "no arc_done.json and not in failures_arc.json"
        return row

    # ---- Post-ARC analysis stage ----
    if not has_analysis:
        a_status = analysis_failures.get(dam_id)
        if a_status:
            row["terminal_stage"] = "analysis_failed"
            row["terminal_reason"] = _bucket_analysis_reason(a_status)
            row["raw_reason"] = a_status
        else:
            row["terminal_stage"] = "analysis_not_run"
            row["terminal_reason"] = "arc done but no analysis_summary.json"
        return row

    if summary_unreadable:
        row["terminal_stage"] = "analysis_summary_unreadable"
        row["terminal_reason"] = "analysis_summary.json present but unparseable (partial write?)"
        return row

    # facts is non-None past here.
    assert facts is not None

    if _is_sentinel(facts["dam_height_m"]):
        row["terminal_stage"] = "bad_dam_height"
        row["terminal_reason"] = (
            f"dam_height_m == {facts['dam_height_m']!r} — height estimator failed "
            f"(every downstream XS will sentinel out)"
        )
        return row

    if facts["any_xs_sentinel"]:
        row["terminal_stage"] = "solver_sentinel_xs"
        row["terminal_reason"] = (
            f"{facts['n_xs_valid']}/{facts['n_xs_total']} XS produced values; "
            f"at least one Qmin_cms / Qmax_cms / P_height_m is -9999 or NaN — "
            f"envelope is polluted"
        )
        return row

    qmin_env = facts["qmin_env"]
    qmax_env = facts["qmax_env"]
    if qmin_env is None or qmax_env is None:
        row["terminal_stage"] = "solver_no_valid_xs"
        row["terminal_reason"] = "analysis ran but no XS produced both Qmin and Qmax"
        return row

    if qmin_env == qmax_env:
        row["terminal_stage"] = "solver_degenerate"
        row["terminal_reason"] = f"Qmin_env == Qmax_env == {qmin_env:g} cms"
        return row

    # Bracket-collapse interpretation:
    #   * full collapse (Qmin == floor AND Qmax == ceiling) → the residual
    #     never crossed inside the bracket; this is a legitimate physical
    #     finding (every flow in the search range is dangerous, or every
    #     flow is safe), so treat it as ok with an annotation.
    #   * half collapse (one end pinned, the other a real root) → one
    #     intercept couldn't be located inside the bracket and was filled
    #     in with the endpoint. Flag.
    floor = facts["bracket_floor"]
    ceiling = facts["bracket_ceiling"]
    pinned_lo = _close(qmin_env, floor)
    pinned_hi = _close(qmax_env, ceiling)
    if pinned_lo and not pinned_hi:
        row["terminal_stage"] = "solver_half_collapse_low"
        row["terminal_reason"] = (
            f"Qmin_env == bracket floor ({qmin_env:g} cms) — conjugate intercept "
            f"never found below rp100"
        )
        return row
    if pinned_hi and not pinned_lo:
        row["terminal_stage"] = "solver_half_collapse_high"
        row["terminal_reason"] = (
            f"Qmax_env == bracket ceiling ({qmax_env:g} cms) — flip intercept "
            f"never found below rp100"
        )
        return row
    if pinned_lo and pinned_hi:
        row["terminal_stage"] = "ok"
        row["terminal_reason"] = (
            "ok (bracket fully spans the dangerous range — neither intercept "
            "crossed inside [floor, ceiling])"
        )
        return row

    if facts["bracket_source"] == "baseflow_x1000":
        row["terminal_stage"] = "ok"
        row["terminal_reason"] = "ok (bracket used baseflow_x1000 fallback — rp100 missing)"
        return row

    row["terminal_stage"] = "ok"
    row["terminal_reason"] = ""
    return row


# ---------------------------------------------------------------------------
# Per-staging-dir aggregation
# ---------------------------------------------------------------------------

def _diagnose_staging_dir(staging_dir: Path) -> List[Dict[str, object]]:
    manifest = _read_json(staging_dir / "tile_manifest.json") or {}
    dam_ids = [int(k) for k in (manifest.get("dam_tiles") or {}).keys()]
    dam_comids = {
        int(k): (int(v) if v is not None else None)
        for k, v in (manifest.get("dam_comids") or {}).items()
    }
    if not dam_ids:
        return []

    arc_failures: Dict[int, str] = {
        int(k): v for k, v in (_read_json(staging_dir / "failures_arc.json") or {}).items()
    }
    analysis_failures: Dict[int, str] = {
        int(k): v for k, v in (_read_json(staging_dir / "failures_analysis.json") or {}).items()
    }

    rows: List[Dict[str, object]] = []
    for dam_id in dam_ids:
        rows.append(_classify_dam(
            dam_id, dam_comids.get(dam_id), staging_dir,
            arc_failures, analysis_failures,
        ))
    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-dir", required=True, type=Path,
                        help="Either a flat staging dir or a parent root with "
                             "huc<level>_<KEY>/ subdirs.")
    # --dams-csv is kept as an old alias of --dams-table.
    parser.add_argument("--dams-table", "--dams-csv", dest="dams_table", type=Path,
                        default=DEFAULT_DAMS_TABLE,
                        help=f"Master dam table (.parquet) used to join HUC8 + display fields "
                             f"(default: {DEFAULT_DAMS_TABLE})")
    parser.add_argument("--output-csv", type=Path, default=None,
                        help="Output CSV path (default: <staging-dir>/diagnostics.csv)")
    args = parser.parse_args()

    staging_dirs = _discover_staging_dirs(args.staging_dir)
    if not staging_dirs:
        sys.exit(f"No staging dirs found under {args.staging_dir}")

    print(f"Diagnosing {len(staging_dirs)} staging dir(s) under {args.staging_dir}")
    all_rows: List[Dict[str, object]] = []
    for staging_dir in staging_dirs:
        rows = _diagnose_staging_dir(staging_dir)
        print(f"  {staging_dir.name}: {len(rows)} dams")
        all_rows.extend(rows)

    if not all_rows:
        print("Nothing to write.")
        return

    df = pd.DataFrame(all_rows)

    # Join HUC8 + a friendly display column from the master CSV when available.
    if args.dams_table.exists():
        try:
            # Read only the join columns that exist in this table.
            want = ["OBJECTID", "HUC8", "Dam_Name", "OSM_Name",
                    "State Abbreviation", "Reach_ID"]
            import pyarrow.parquet as _pq  # column names from the file footer
            names = _pq.ParquetFile(args.dams_table).schema_arrow.names
            dams = read_dam_table(args.dams_table,
                                  columns=[c for c in want if c in names])
            dams["OBJECTID"] = pd.to_numeric(dams["OBJECTID"], errors="coerce")
            dams = dams.dropna(subset=["OBJECTID"])
            dams["OBJECTID"] = dams["OBJECTID"].astype(int)
            df = df.merge(dams, on="OBJECTID", how="left")
        except Exception as e:
            print(f"  (master dam table join skipped: {e})")

    out_path = args.output_csv or (args.staging_dir / "diagnostics.csv")
    df.to_csv(out_path, index=False)
    print(f"\nWrote {len(df)} rows → {out_path}")

    tally = df.groupby(["terminal_stage", "terminal_reason"]).size().reset_index(name="count")
    tally = tally.sort_values("count", ascending=False)
    print("\nTally:")
    for _, row in tally.iterrows():
        stage = row["terminal_stage"]
        reason = row["terminal_reason"] or "—"
        print(f"  {stage:<28} {row['count']:>6}   {reason}")


if __name__ == "__main__":
    main()
