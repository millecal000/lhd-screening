#!/usr/bin/env python3
"""
Enrich data/full_lhd_website.parquet with dam dimensions from the National Inventory of Dams.

For every dam, find its NID counterpart and copy over heights, crest length,
and spillway width. Matching uses two methods, in priority order:

  1. federal_id  — our Federal_ID exactly matches an NID "Federal ID" (high
                   confidence; no distance check).
  2. coordinate  — nearest NID dam within --radius meters by haversine
                   (default 50 m, the same threshold the dataset uses for
                   dedup).

Values fill the canonical columns only when those are blank; existing values
are never overwritten. Provenance is recorded in NID_Match_Method /
NID_Match_Federal_ID / NID_Match_Dist_M.

The NID national CSV (~92k dams) is downloaded once and cached locally. Use
--nid-csv to point at an existing copy.

    python scripts/enrich_nid_height.py                 # download + match
    python scripts/enrich_nid_height.py --radius 100    # looser match
    python scripts/enrich_nid_height.py --out /tmp/preview.parquet

Requirements: pandas, numpy, scikit-learn, requests
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

# Make backend/ importable so this script uses the shared dam-table module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table, write_dam_table  # noqa: E402

TABLE_DEFAULT = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
NID_CACHE_DEFAULT = Path(__file__).resolve().parent.parent / "frontend" / "data" / "nid_nation.csv"
NID_URL = "https://nid.sec.usace.army.mil/api/nation/csv"
EARTH_RADIUS_M = 6_371_000.0

# (NID source column, canonical column in full_lhd_website.parquet)
NID_FIELDS = [
    ("Dam Height (Ft)", "Dam_Height_Ft"),
    ("Hydraulic Height (Ft)", "Hydraulic_Height_Ft"),
    ("Structural Height (Ft)", "Structural_Height_Ft"),
    ("NID Height (Ft)", "NID_Height_Ft"),
    ("Dam Length (Ft)", "Dam_Length_Ft"),
    ("Spillway Width (Ft)", "Spillway_Width_Ft"),
]


def load_nid(nid_csv: Path) -> tuple[pd.DataFrame, str]:
    if not nid_csv.exists():
        print(f"Downloading NID national CSV → {nid_csv} …")
        import requests

        resp = requests.get(NID_URL, timeout=180)
        resp.raise_for_status()
        nid_csv.write_bytes(resp.content)

    # First line is a "Data Last Updated:" preamble; real header is line 2.
    with open(nid_csv, newline="") as f:
        preamble = f.readline()
    dataset_date = preamble.split(",", 1)[-1].strip()

    nid = pd.read_csv(nid_csv, dtype=str, skiprows=1, low_memory=False, encoding="latin-1")
    print(f"  loaded {len(nid):,} NID dams (dataset updated {dataset_date})")

    nid["_lat"] = pd.to_numeric(nid["Latitude"], errors="coerce")
    nid["_lng"] = pd.to_numeric(nid["Longitude"], errors="coerce")
    return nid, dataset_date


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # --csv is kept as an old alias of --table.
    parser.add_argument("--table", "--csv", dest="table", type=Path, default=TABLE_DEFAULT,
                        help="Path to full_lhd_website.parquet (default: %(default)s)")
    parser.add_argument("--nid-csv", type=Path, default=NID_CACHE_DEFAULT,
                        help="NID national CSV; downloaded here if missing")
    parser.add_argument("--radius", type=float, default=50.0,
                        help="Coordinate-match radius in meters (default: 50)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output .parquet path (default: overwrite input)")
    args = parser.parse_args()

    out_path: Path = args.out or args.table
    if not args.table.exists():
        sys.exit(f"ERROR: dam table not found at {args.table}")

    print(f"Loading {args.table} …")
    # as_str=True: every cell as text, as the old read_csv(dtype=str) gave it.
    df = read_dam_table(args.table, as_str=True)
    print(f"  {len(df):,} rows")

    nid, nid_dataset_date = load_nid(args.nid_csv)

    # Build a Federal ID → NID row lookup (first occurrence wins).
    nid_fed = nid["Federal ID"].fillna("").str.strip()
    fed_index: dict[str, int] = {}
    for pos, fid in enumerate(nid_fed):
        if fid and fid not in fed_index:
            fed_index[fid] = pos

    # BallTree over NID dams with valid coordinates (haversine wants radians).
    nid_geo = nid[nid["_lat"].notna() & nid["_lng"].notna()]
    nid_geo_pos = nid_geo.index.to_numpy()
    tree = BallTree(np.radians(nid_geo[["_lat", "_lng"]].to_numpy()), metric="haversine")

    # Prepare output columns.
    n = len(df)
    method = [""] * n
    matched_fid = [""] * n
    matched_dist = [""] * n
    nid_values: dict[str, list[str]] = {canon: [""] * n for _, canon in NID_FIELDS}

    def record(row_pos: int, nid_pos: int) -> None:
        for nid_col, canon in NID_FIELDS:
            val = nid.iat[nid_pos, nid.columns.get_loc(nid_col)]
            nid_values[canon][row_pos] = "" if pd.isna(val) else str(val).strip()
        fid = nid.iat[nid_pos, nid.columns.get_loc("Federal ID")]
        matched_fid[row_pos] = "" if pd.isna(fid) else str(fid).strip()

    our_fed = df["Federal_ID"].fillna("").str.strip()
    lat = pd.to_numeric(df["Latitude"], errors="coerce")
    lng = pd.to_numeric(df["Longitude"], errors="coerce")

    radius_rad = args.radius / EARTH_RADIUS_M
    n_fed = n_coord = 0

    # Spatial query for all coordinate candidates at once.
    coord_mask = lat.notna() & lng.notna()
    coord_pos = np.where(coord_mask.to_numpy())[0]
    if len(coord_pos):
        dist_rad, idx = tree.query(
            np.radians(np.column_stack([lat.to_numpy()[coord_pos], lng.to_numpy()[coord_pos]])),
            k=1,
        )
        nearest_dist_m = dist_rad[:, 0] * EARTH_RADIUS_M
        nearest_nid_pos = nid_geo_pos[idx[:, 0]]
    else:
        nearest_dist_m = np.array([])
        nearest_nid_pos = np.array([])

    spatial_lookup = {
        int(rp): (float(nearest_dist_m[i]), int(nearest_nid_pos[i]))
        for i, rp in enumerate(coord_pos)
    }

    for row_pos in range(n):
        fid = our_fed.iat[row_pos]
        if fid and fid in fed_index:
            record(row_pos, fed_index[fid])
            method[row_pos] = "federal_id"
            matched_dist[row_pos] = "0"
            n_fed += 1
            continue

        hit = spatial_lookup.get(row_pos)
        if hit is not None and hit[0] <= args.radius:
            dist_m, nid_pos = hit
            record(row_pos, nid_pos)
            method[row_pos] = "coordinate"
            matched_dist[row_pos] = f"{dist_m:.1f}"
            n_coord += 1

    df["NID_Match_Method"] = method
    df["NID_Match_Federal_ID"] = matched_fid
    df["NID_Match_Dist_M"] = matched_dist
    df["NID_Dataset_Updated"] = nid_dataset_date

    # Fill blanks in the canonical columns from NID. Never overwrite existing values.
    fill_counts: dict[str, int] = {}
    for _, canon in NID_FIELDS:
        if canon not in df.columns:
            df[canon] = ""
        existing = df[canon].fillna("").astype(str).str.strip()
        incoming = pd.Series(nid_values[canon], index=df.index)
        blank_mask = existing == ""
        fill_mask = blank_mask & (incoming != "")
        df.loc[fill_mask, canon] = incoming[fill_mask]
        fill_counts[canon] = int(fill_mask.sum())

    has_any = sum(
        1 for i in range(n)
        if method[i] and any(nid_values[c][i] for _, c in NID_FIELDS)
    )

    # write_dam_table turns the text back into the CSV-era column types.
    write_dam_table(df, out_path)
    print(
        f"\nDone — {n_fed:,} federal-id + {n_coord:,} coordinate matches "
        f"({n_fed + n_coord:,} total, {has_any:,} carry at least one NID value)"
    )
    for _, canon in NID_FIELDS:
        print(f"  filled {canon:24s} blanks: {fill_counts[canon]:>5d}")
    print(f"Output → {out_path}")


if __name__ == "__main__":
    main()
