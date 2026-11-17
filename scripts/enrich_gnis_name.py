#!/usr/bin/env python3
"""
One-time script to enrich data/full_lhd_website.parquet with GNIS_Name from NHDPlus.

Adds a GNIS_Name column by finding the nearest NHDPlus flowline for each dam.
Two modes:

  1. Local file (recommended — fast, works offline):
       python scripts/enrich_gnis_name.py --nhd-file /path/to/NHDFlowline.gpkg

     Download NHDPlus Medium Resolution flowlines from:
       https://www.usgs.gov/national-hydrography/access-national-hydrography-products

  2. API mode (no download required, queries USGS WFS by state bbox, slower):
       python scripts/enrich_gnis_name.py

     Requires: pip install pynhd

Requirements (both modes):
    pip install geopandas pandas pyproj shapely
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

# Make backend/ importable so this script uses the shared dam-table module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table, write_dam_table  # noqa: E402

TABLE_DEFAULT = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet

# Nearest-neighbor search radius in degrees (~2 km at mid-latitudes).
# Dams further than this from any NHDPlus flowline will get an empty GNIS_Name.
MAX_DIST_DEG = 0.02


# ---------------------------------------------------------------------------
# Mode 1: local NHDPlus file
# ---------------------------------------------------------------------------

def enrich_from_local(dams_gdf: gpd.GeoDataFrame, nhd_path: Path) -> list[str]:
    print(f"Loading NHDPlus from {nhd_path} …")
    nhd = gpd.read_file(nhd_path)

    # Normalize GNIS_Name column (NHDPlus HR uses "gnis_name"; MR uses "GNIS_Name")
    col_map = {c: "GNIS_Name" for c in nhd.columns if c.lower() == "gnis_name"}
    if not col_map:
        sys.exit("ERROR: NHDPlus file has no GNIS_Name column.")
    nhd = nhd.rename(columns=col_map)

    nhd = nhd[["GNIS_Name", "geometry"]].to_crs("EPSG:4326")
    nhd = nhd[nhd["GNIS_Name"].notna() & (nhd["GNIS_Name"].str.strip() != "")]
    print(f"  {len(nhd):,} NHDPlus features with a non-empty GNIS_Name")

    print("Running spatial nearest-neighbor join …")
    joined = gpd.sjoin_nearest(
        dams_gdf[["geometry"]],
        nhd,
        how="left",
        max_distance=MAX_DIST_DEG,
    )
    name_col = "GNIS_Name_right" if "GNIS_Name_right" in joined.columns else "GNIS_Name"
    return joined[name_col].fillna("").tolist()


# ---------------------------------------------------------------------------
# Mode 2: USGS WFS API via pynhd
# ---------------------------------------------------------------------------

def _fetch_state_flowlines(wd, bbox_geom) -> gpd.GeoDataFrame | None:
    """Query NHDPlus WFS for a bounding box; return None on failure."""
    try:
        fl = wd.bygeom(bbox_geom, geo_crs="EPSG:4326")
        return fl
    except Exception as exc:
        print(f"    WARNING: WFS query failed — {exc}")
        return None


def enrich_from_api(dams_gdf: gpd.GeoDataFrame) -> list[str]:
    try:
        from pynhd import WaterData
    except ImportError:
        sys.exit("pynhd is not installed. Run:  pip install pynhd")

    wd = WaterData("nhdflowline_network")
    gnis_names: list[str] = [""] * len(dams_gdf)

    states = dams_gdf["State Abbreviation"].dropna().unique()
    print(f"Querying NHDPlus WFS for {len(states)} states …")

    for state in sorted(states):
        mask = dams_gdf["State Abbreviation"] == state
        state_dams = dams_gdf[mask]
        if state_dams.empty:
            continue

        print(f"  {state}: {len(state_dams)} dams", end="", flush=True)

        # Build a slightly expanded bounding box for edge dams
        mn_x, mn_y, mx_x, mx_y = state_dams.total_bounds
        bbox_geom = box(mn_x - 0.05, mn_y - 0.05, mx_x + 0.05, mx_y + 0.05)

        fl = _fetch_state_flowlines(wd, bbox_geom)
        if fl is None or fl.empty:
            print(" — no features returned")
            continue

        # Normalize column name
        col_map = {c: "GNIS_Name" for c in fl.columns if c.lower() == "gnis_name"}
        fl = fl.rename(columns=col_map)
        if "GNIS_Name" not in fl.columns:
            print(" — GNIS_Name column missing in response")
            continue

        fl = fl[fl["GNIS_Name"].notna() & (fl["GNIS_Name"].str.strip() != "")].to_crs("EPSG:4326")
        if fl.empty:
            print(" — all features have empty GNIS_Name")
            continue

        joined = gpd.sjoin_nearest(
            state_dams[["geometry"]],
            fl[["GNIS_Name", "geometry"]],
            how="left",
            max_distance=MAX_DIST_DEG,
        )
        name_col = "GNIS_Name_right" if "GNIS_Name_right" in joined.columns else "GNIS_Name"

        for pos, (orig_idx, row) in enumerate(joined.iterrows()):
            name = row[name_col]
            loc = dams_gdf.index.get_loc(orig_idx)
            gnis_names[loc] = "" if pd.isna(name) else str(name)

        n_found = sum(1 for i in dams_gdf.index[mask] if gnis_names[dams_gdf.index.get_loc(i)])
        print(f" — {n_found} matched")
        time.sleep(0.25)  # be polite to the WFS endpoint

    return gnis_names


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --csv is kept as an old alias of --table.
    parser.add_argument("--table", "--csv", dest="table", type=Path, default=TABLE_DEFAULT,
                        help="Path to full_lhd_website.parquet (default: %(default)s)")
    parser.add_argument("--nhd-file", type=Path, default=None,
                        help="Local NHDPlus GeoPackage or shapefile (use instead of API)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output .parquet path (default: overwrite input)")
    args = parser.parse_args()

    table_path: Path = args.table
    out_path: Path = args.out or table_path

    if not table_path.exists():
        sys.exit(f"ERROR: dam table not found at {table_path}")

    print(f"Loading {table_path} …")
    # as_str=True: every cell as text, as the old read_csv(dtype=str) gave it.
    df = read_dam_table(table_path, as_str=True)
    print(f"  {len(df):,} rows")

    # Build GeoDataFrame for dams with valid coordinates
    df["_lat"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["_lng"] = pd.to_numeric(df["Longitude"], errors="coerce")
    valid_mask = df["_lat"].notna() & df["_lng"].notna()

    dams_gdf = gpd.GeoDataFrame(
        df[valid_mask].copy(),
        geometry=gpd.points_from_xy(df.loc[valid_mask, "_lng"], df.loc[valid_mask, "_lat"]),
        crs="EPSG:4326",
    )
    print(f"  {len(dams_gdf):,} dams have valid coordinates")

    if args.nhd_file:
        if not args.nhd_file.exists():
            sys.exit(f"ERROR: NHD file not found at {args.nhd_file}")
        gnis_values = enrich_from_local(dams_gdf, args.nhd_file)
    else:
        gnis_values = enrich_from_api(dams_gdf)

    df["GNIS_Name"] = ""
    df.loc[valid_mask, "GNIS_Name"] = gnis_values
    df = df.drop(columns=["_lat", "_lng"])

    # write_dam_table turns the text back into the CSV-era column types.
    write_dam_table(df, out_path)

    n_enriched = (df["GNIS_Name"].str.strip() != "").sum()
    print(f"\nDone — {n_enriched:,}/{len(df):,} dams now have GNIS_Name")
    print(f"Output → {out_path}")


if __name__ == "__main__":
    main()
