"""Fetch LHDI review fields from the NID API and merge them into the master dam table.

For every row in ``data/full_lhd_website.parquet`` that has an ``LHD_ID``,
hit ``GET https://nid.sec.usace.army.mil/api/lowhead-dams/{id}/inventory`` and
pull four fields:

    reviewStatus         -> Review_Status         (column already exists; refreshed)
    lhdReviewer          -> LHD_Reviewer          (new)
    reviewNotes          -> Review_Notes          (new)
    reviewNotesDetails   -> Review_Notes_Details  (new)

A timestamped backup of the master table is written before merging
(``full_lhd_website.bak.<YYYYmmdd_HHMMSS>.parquet`` in the same folder).

Usage
    python backend/fetch_lhd_review_metadata.py [--table data/full_lhd_website.parquet]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# Master dam table (Parquet) reader/writer shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table, write_dam_table

# Was data/full_lhd_website.csv, opened with Windows' default encoding (cp1252).
MASTER_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
ENDPOINT = "https://nid.sec.usace.army.mil/api/lowhead-dams/{lhd_id}/inventory"
WORKERS = 16
TIMEOUT = 30
REVIEW_FIELDS = ("reviewStatus", "lhdReviewer", "reviewNotes", "reviewNotesDetails",
                 "updated", "lastEditedDate")
COLUMN_MAP = {
    "reviewStatus":      "Review_Status",
    "lhdReviewer":       "LHD_Reviewer",
    "reviewNotes":       "Review_Notes",
    "reviewNotesDetails":"Review_Notes_Details",
    "updated":           "LHDI_Updated",
    "lastEditedDate":    "LHDI_Last_Edited",
}


def _cell_text(v) -> str:
    """A table cell as the text the old csv.DictReader saw ("" when missing)."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
        return ""
    return str(v)


def normalize_lhd_id(raw) -> str | None:
    raw = _cell_text(raw).strip()
    if not raw:
        return None
    try:
        return str(int(float(raw)))
    except ValueError:
        return None


def fetch_one(session: requests.Session, lhd_id: str) -> tuple[str, dict | None, str | None]:
    url = ENDPOINT.format(lhd_id=lhd_id)
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return lhd_id, None, f"HTTP {r.status_code}"
        data = r.json()
        return lhd_id, {f: data.get(f) for f in REVIEW_FIELDS}, None
    except Exception as e:
        return lhd_id, None, repr(e)


def fetch_all(lhd_ids: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    errors: list[tuple[str, str]] = []
    start = time.time()
    with requests.Session() as session, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(fetch_one, session, lid) for lid in lhd_ids]
        for i, fut in enumerate(as_completed(futures), 1):
            lid, fields, err = fut.result()
            if fields is not None:
                results[lid] = fields
            else:
                errors.append((lid, err or "unknown"))
            if i % 500 == 0 or i == len(futures):
                elapsed = time.time() - start
                print(f"  {i}/{len(futures)}  ok={len(results)}  err={len(errors)}  {elapsed:.1f}s", flush=True)
    if errors:
        print(f"\n{len(errors)} errors. First 10:")
        for lid, e in errors[:10]:
            print(f"  {lid}: {e}")
    return results


def merge_into_table(table_path: Path, results: dict[str, dict]) -> None:
    """Write the fetched review fields into the dam table (same rules as before)."""
    # Backup keeps the .parquet suffix so it can be opened like the original.
    backup = table_path.with_name(
        f"{table_path.stem}.bak.{datetime.now():%Y%m%d_%H%M%S}{table_path.suffix}")
    shutil.copy2(table_path, backup)
    print(f"backup -> {backup.name}")

    df = read_dam_table(table_path)

    # Add missing columns right after Review_Status (or at the end), in the
    # same order as before.
    new_cols = [COLUMN_MAP[f] for f in REVIEW_FIELDS if COLUMN_MAP[f] not in df.columns]
    if "Review_Status" not in df.columns:
        df["Review_Status"] = np.nan
    insert_at = list(df.columns).index("Review_Status") + 1
    for col in new_cols:
        if col not in df.columns:
            df.insert(insert_at, col, np.nan)
            insert_at += 1

    # The six review columns receive text. Hold them as Python objects while
    # filling; write_dam_table then gives them their CSV-era types again
    # (empty -> missing, all-number columns -> numbers).
    for col in COLUMN_MAP.values():
        df[col] = df[col].astype(object)

    updated = 0
    diffs_review_status = 0
    lhd_ids = df["LHD_ID"].map(normalize_lhd_id) if "LHD_ID" in df.columns else \
        pd.Series([None] * len(df), index=df.index)
    for idx, lid in lhd_ids.items():
        if not lid or lid not in results:
            continue  # untouched rows keep their values (new columns stay empty)
        fields = results[lid]
        new_rs = fields.get("reviewStatus") or ""
        old_rs = _cell_text(df.at[idx, "Review_Status"])
        if new_rs != old_rs:
            diffs_review_status += 1
        for api_field, col in COLUMN_MAP.items():
            val = fields.get(api_field)
            df.at[idx, col] = "" if val is None else str(val)
        updated += 1

    write_dam_table(df, table_path)

    print(f"rows updated: {updated}")
    print(f"Review_Status values that changed: {diffs_review_status}")
    print(f"new columns added: {new_cols}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", type=Path, default=MASTER_TABLE,
                    help=f"dam table (.parquet) to update in place (default: {MASTER_TABLE})")
    args = ap.parse_args()

    if not args.table.exists():
        print(f"missing: {args.table}", file=sys.stderr)
        return 1

    # Unique LHD_IDs in table order (only the one column is read).
    lhd_ids: list[str] = []
    seen: set[str] = set()
    for raw in read_dam_table(args.table, columns=["LHD_ID"])["LHD_ID"]:
        lid = normalize_lhd_id(raw)
        if lid and lid not in seen:
            seen.add(lid)
            lhd_ids.append(lid)
    print(f"unique LHD_IDs to fetch: {len(lhd_ids)}")

    results = fetch_all(lhd_ids)
    print(f"\nfetched {len(results)}/{len(lhd_ids)} records\n")

    merge_into_table(args.table, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
