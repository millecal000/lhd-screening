"""
One-time conversion of the master dam table from CSV to Parquet.

    uv run --frozen python scripts/convert_dam_table_to_parquet.py

Reads  data/full_lhd_website.csv      (strictly UTF-8; stops if it is not)
Writes data/full_lhd_website.parquet  (through lhd_processor.dam_table)

After writing, it reads the Parquet back and checks that every row, column,
value and column type matches the CSV, and that the special characters
(ñ, ’, ...) are intact. It prints PASS or stops with what differs.

It never overwrites an existing Parquet file unless --force is given, and it
never changes or deletes the CSV; removing the CSV from the repository is a
separate, deliberate step (git rm) once you have checked the result.

Options:
    --csv PATH     source CSV  (default: data/full_lhd_website.csv)
    --out PATH     output Parquet (default: the CSV path with .parquet)
    --force        overwrite an existing output file
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Make backend/ importable when run from the repository root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "backend"))

from lhd_processor.dam_table import read_dam_table, write_dam_table  # noqa: E402

DEFAULT_CSV = _REPO_ROOT / "data" / "full_lhd_website.csv"


def count_special_chars(df: pd.DataFrame) -> int:
    """Number of non-ASCII characters in all text cells (e.g. ñ, ’)."""
    total = 0
    for c in df.columns:
        s = df[c]
        if s.dtype == object or pd.api.types.is_string_dtype(s.dtype):
            for v in s.dropna():
                total += sum(1 for ch in str(v) if ord(ch) > 127)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="source CSV")
    ap.add_argument("--out", type=Path, default=None, help="output .parquet")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output")
    args = ap.parse_args()

    src: Path = args.csv
    out: Path = args.out or src.with_suffix(".parquet")
    if not src.exists():
        sys.exit(f"Source CSV not found: {src}")
    if out.exists() and not args.force:
        sys.exit(f"{out} already exists. Use --force to overwrite it.")

    # 1. Read the CSV strictly as UTF-8 ("utf-8-sig" also accepts the marker
    #    Excel adds to "CSV UTF-8" files). A file in any other encoding stops
    #    here instead of being silently garbled.
    print(f"Reading {src}")
    try:
        csv_df = pd.read_csv(src, encoding="utf-8-sig", low_memory=False)
    except UnicodeDecodeError as e:
        sys.exit(f"{src} is not valid UTF-8 ({e}).\n"
                 f"If it was saved from Excel, re-save it as 'CSV UTF-8 (Comma delimited)'.")
    print(f"  {len(csv_df):,} rows x {len(csv_df.columns)} columns, "
          f"{count_special_chars(csv_df)} special characters")

    # Line breaks inside a text cell (a few review notes have them) arrive as
    # Windows "\r\n" when git checked the CSV out on Windows, and as "\n" on a
    # Mac. Store them as "\n" so the Parquet file is the same wherever it was
    # converted.
    n_crlf = 0
    for c in csv_df.columns:
        s = csv_df[c]
        if s.dtype == object or pd.api.types.is_string_dtype(s.dtype):
            has = s.str.contains("\r", regex=False, na=False)
            if has.any():
                n_crlf += int(has.sum())
                csv_df[c] = s.str.replace("\r\n", "\n", regex=False).str.replace("\r", "\n", regex=False)
    if n_crlf:
        print(f"  normalised Windows line breaks inside {n_crlf} text cell(s)")

    # 2. Write through the shared module (same code every script uses).
    write_dam_table(csv_df, out)
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.2f} MB; "
          f"CSV was {src.stat().st_size / 1e6:.2f} MB)")

    # 3. Verify: read back and compare everything.
    pq_df = read_dam_table(out)
    problems = []
    if pq_df.shape != csv_df.shape:
        problems.append(f"shape {pq_df.shape} != {csv_df.shape}")
    if list(pq_df.columns) != list(csv_df.columns):
        problems.append("column names or order differ")
    else:
        type_diff = [c for c in csv_df.columns if pq_df[c].dtype != csv_df[c].dtype]
        if type_diff:
            problems.append(f"column types differ: {type_diff}")
        if not pq_df.equals(csv_df):
            problems.append("values differ")
    n_csv, n_pq = count_special_chars(csv_df), count_special_chars(pq_df)
    if n_csv != n_pq:
        problems.append(f"special characters {n_pq} != {n_csv}")

    if problems:
        print("FAIL:\n  " + "\n  ".join(problems))
        return 1
    print(f"PASS: {len(pq_df):,} rows, {len(pq_df.columns)} columns, identical values "
          f"and types, {n_pq} special characters intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
