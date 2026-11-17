"""
Read and write the master dam table (``data/full_lhd_website.parquet``).

Every script that touches the dam table goes through this module, so there
is one place that decides the file format and the column types.

Why Parquet
-----------
The table used to be ``data/full_lhd_website.csv``. CSV is plain text, so
every reader has to be told how the text is encoded. Several scripts read it
as latin-1 while it was actually UTF-8 and then wrote it back as UTF-8; each
round trip doubled the bytes of its 41 special characters (``ñ``, ``’``),
and in November 2026 a full pipeline run grew its copy to 48.9 GB.

Parquet stores text as UTF-8 by definition, with no encoding setting to get
wrong, so that failure cannot happen. It also stores each column's type, which
is what the rest of this module is careful about.

Column types
------------
With CSV, every read re-guessed the column types (numbers vs text). With
Parquet the types are stored, so a script that writes a number column back as
text (several scripts read the table with ``dtype=str``) would change that
column to text permanently and break later arithmetic, e.g.
``Dam_Height_WSP_Ft > 0``.

``write_dam_table`` therefore re-applies exactly the rules pandas used when
it read the CSV, but only to text columns (and pandas' nullable ``Int64`` /
``Float64`` / ``boolean`` columns, e.g. ``Reach_ID`` after
``assign_nhd_comid.py``) and only in memory:

* empty text becomes missing (CSV could not tell them apart either);
* a text column whose every value is a number becomes a number column
  (int64 when there are no gaps, float64 otherwise), as ``pd.read_csv`` did;
* a column mixing text and numbers becomes all text (Parquet cannot store a
  mix; pyarrow would refuse with ``ArrowTypeError``);
* a nullable ``Int64`` column becomes ``int64`` without gaps or ``float64``
  with gaps, exactly what the CSV produced on the next read.

Plain number columns (int64, float64) are left untouched. The result: the Parquet file always has
the same column types the pipeline saw when it read the old CSV.

Writing safely
--------------
The table is written to a temporary file in the same folder and then swapped
into place with ``os.replace``, so a crash can never leave a half-written
table. On Windows the swap fails while another program (Excel, an antivirus
scan, a file preview) holds the file open; it is retried for about 10 seconds
and then fails with a clear message.
"""
from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

# The repository's master dam table.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DAM_TABLE = REPO_ROOT / "data" / "full_lhd_website.parquet"

# Parquet engine and compression. pyarrow is named explicitly because the
# environment also has fastparquet installed, and pandas' "auto" choice could
# otherwise differ between machines. Snappy is pyarrow's default and is read
# natively by the website's JavaScript Parquet reader (hyparquet).
_ENGINE = "pyarrow"
_COMPRESSION = "snappy"

# os.replace retries when Windows reports the destination as locked.
_REPLACE_TRIES = 5
_REPLACE_WAIT_S = 2.0


class DamTableFormatError(ValueError):
    """Raised when a dam-table path is not a Parquet file."""


def _check_parquet_path(path: Path) -> None:
    """Refuse anything but a .parquet path, with a pointer to the converter."""
    if path.suffix.lower() == ".csv":
        raise DamTableFormatError(
            f"{path} is a CSV file. The dam table is now stored as Parquet.\n"
            f"Convert it once with:\n"
            f"  uv run --frozen python scripts/convert_dam_table_to_parquet.py "
            f"--csv \"{path}\" --out \"{path.with_suffix('.parquet')}\"\n"
            f"then point this command at the .parquet file."
        )
    if path.suffix.lower() != ".parquet":
        raise DamTableFormatError(f"{path}: expected a .parquet dam table.")


def _is_text_column(s: pd.Series) -> bool:
    """True for columns holding text (object dtype, or pandas' string dtype)."""
    return s.dtype == object or pd.api.types.is_string_dtype(s.dtype)


def _needs_csv_rules(s: pd.Series) -> bool:
    """Columns whose stored type must be re-derived the way read_csv did.

    Text columns, plus pandas' nullable extension types (Int64, Float64,
    boolean). Those never came out of read_csv, so writing them as-is would
    give the Parquet file a type the CSV-era pipeline never saw.
    """
    return _is_text_column(s) or isinstance(s.dtype, pd.api.extensions.ExtensionDtype)


def read_dam_table(
    path: Path | str = DEFAULT_DAM_TABLE,
    columns: Optional[Iterable[str]] = None,
    as_str: bool = False,
) -> pd.DataFrame:
    """Read a dam table from Parquet.

    path    : a .parquet file (the master table, a run copy, or a per-watershed
              subset). A .csv path raises DamTableFormatError.
    columns : read only these columns (faster; None = all).
    as_str  : return every value as text, like ``pd.read_csv(..., dtype=str)``:
              numbers become their text form (``35.21343``, ``13080001``,
              ``78840.0``), missing values stay missing (NaN). For scripts that
              were written to treat every cell as a string.
    """
    path = Path(path)
    _check_parquet_path(path)
    df = pd.read_parquet(path, engine=_ENGINE,
                         columns=list(columns) if columns is not None else None)
    if as_str:
        df = df.apply(_to_str_column)
    return df


def _to_str_column(s: pd.Series) -> pd.Series:
    """One column as text the way read_csv(dtype=str) produced it."""
    out = s.astype(object)
    mask = s.notna()
    out[mask] = s[mask].map(str)
    out[~mask] = np.nan
    return out


def normalize_dam_table_types(df: pd.DataFrame) -> pd.DataFrame:
    """Re-apply the CSV-era type rules to the text columns (see module notes).

    Implemented as an in-memory CSV round trip of only the text columns:
    the text is written to a Python string and parsed back with
    ``pd.read_csv``, so the resulting types are exactly what reading the old
    CSV produced. No file is touched and no encoding is involved (the string
    never leaves memory).
    """
    text_cols = [c for c in df.columns if _needs_csv_rules(df[c])]
    if not text_cols:
        return df
    buf = io.StringIO()
    df[text_cols].to_csv(buf, index=False)
    buf.seek(0)
    reparsed = pd.read_csv(buf, low_memory=False)
    reparsed.index = df.index  # keep the caller's row labels (subsets, filters)
    out = df.copy()
    for c in text_cols:
        out[c] = reparsed[c]
    return out


def write_dam_table(df: pd.DataFrame, path: Path | str) -> Path:
    """Write a dam table to Parquet, atomically, with CSV-era column types.

    The DataFrame's row labels are not stored (like ``to_csv(index=False)``).
    Returns the path written.
    """
    path = Path(path)
    _check_parquet_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    table = normalize_dam_table_types(df).reset_index(drop=True)

    # Write next to the target, then swap it in. A crash mid-write leaves the
    # old table intact and at worst a stray *.tmp file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    table.to_parquet(tmp, engine=_ENGINE, compression=_COMPRESSION, index=False)

    last_err: Optional[Exception] = None
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError as e:  # Windows: file open in another program
            last_err = e
            time.sleep(_REPLACE_WAIT_S)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise PermissionError(
        f"Could not replace {path}: it is open in another program "
        f"(Excel, a viewer, antivirus). Close it and rerun. ({last_err})"
    )
