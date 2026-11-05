"""
Recompress per-dam DEM GeoTIFFs in place, losslessly.

For each DEM/<dam_id>/dem_<dam_id>.tif under --root:
  1. Skip it if it is already compressed with the target method (so reruns resume).
  2. Write a compressed copy next to it (<name>.tmp_compress.tif).
  3. Check the copy against the original: same size, data type, georeferencing,
     nodata, and every pixel identical (NaN counts as equal to NaN).
  4. Only if every check passes, rename the copy over the original.
     If anything fails, the copy is deleted and the original is left untouched.

Every file gets one line in a CSV log, so you can see what happened and how much
space was saved.

Run inside the micromamba environment that has GDAL (it needs the osgeo Python
bindings that come with conda-forge's gdal package):

    micromamba activate gdal
    python scripts/compress_dems_in_place.py --root /Volumes/EB406_T7_2/lhd-nwm --dry-run
    python scripts/compress_dems_in_place.py --root /Volumes/EB406_T7_2/lhd-nwm --limit 5
    caffeinate -i python scripts/compress_dems_in_place.py --root /Volumes/EB406_T7_2/lhd-nwm
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

TMP_SUFFIX = ".tmp_compress.tif"
ROWS_PER_CHUNK = 1024  # rows compared at a time, keeps memory low on 4 GB files


def find_dems(root: Path) -> list[Path]:
    """All dem_*.tif files under <root>/huc6_*/DEM/<id>/ (or <root>/DEM/<id>/)."""
    found = []
    for pattern in ("huc*/DEM/*/dem_*.tif", "DEM/*/dem_*.tif"):
        for p in root.glob(pattern):
            name = p.name
            if name.startswith("._") or name.endswith(TMP_SUFFIX):
                continue  # macOS metadata files on exFAT, or our own leftovers
            found.append(p)
    return sorted(set(found))


def current_compression(path: Path) -> str:
    ds = gdal.Open(str(path))
    try:
        return (ds.GetMetadata("IMAGE_STRUCTURE") or {}).get("COMPRESSION", "NONE").upper()
    finally:
        ds = None


def same_pixels(a_path: Path, b_path: Path) -> tuple[bool, str]:
    """Compare two rasters: structure, georeferencing, nodata, then every pixel."""
    a = gdal.Open(str(a_path))
    b = gdal.Open(str(b_path))
    try:
        if (a.RasterXSize, a.RasterYSize, a.RasterCount) != (b.RasterXSize, b.RasterYSize, b.RasterCount):
            return False, "size or band count differs"
        if a.GetGeoTransform() != b.GetGeoTransform():
            return False, "geotransform differs"
        if a.GetProjection() != b.GetProjection():
            return False, "projection differs"
        for i in range(1, a.RasterCount + 1):
            ba, bb = a.GetRasterBand(i), b.GetRasterBand(i)
            if ba.DataType != bb.DataType:
                return False, f"band {i} data type differs"
            if ba.GetNoDataValue() != bb.GetNoDataValue():
                na, nb = ba.GetNoDataValue(), bb.GetNoDataValue()
                if not (na is not None and nb is not None and np.isnan(na) and np.isnan(nb)):
                    return False, f"band {i} nodata differs"
            for y0 in range(0, a.RasterYSize, ROWS_PER_CHUNK):
                rows = min(ROWS_PER_CHUNK, a.RasterYSize - y0)
                xa = ba.ReadAsArray(0, y0, a.RasterXSize, rows)
                xb = bb.ReadAsArray(0, y0, a.RasterXSize, rows)
                equal_nan = np.issubdtype(xa.dtype, np.floating)
                if not np.array_equal(xa, xb, equal_nan=equal_nan):
                    return False, f"band {i} pixels differ near row {y0}"
        return True, "identical"
    finally:
        a = b = None


def compress_one(path_str: str, method: str, dry_run: bool) -> dict:
    path = Path(path_str)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    row = {"path": str(path), "status": "", "old_bytes": "", "new_bytes": "", "detail": ""}
    try:
        row["old_bytes"] = path.stat().st_size
        comp = current_compression(path)
        if comp == method:
            row.update(status="already_done", new_bytes=row["old_bytes"])
            return row
        if dry_run:
            row.update(status="would_compress", detail=f"currently {comp}")
            return row

        if tmp.exists():
            tmp.unlink()  # leftover from an interrupted run

        src = gdal.Open(str(path))
        dtype = gdal.GetDataTypeName(src.GetRasterBand(1).DataType)
        predictor = "3" if dtype.startswith("Float") else "2"
        options = [
            f"COMPRESS={method}",
            f"PREDICTOR={predictor}",
            "TILED=YES",
            "BLOCKXSIZE=512",
            "BLOCKYSIZE=512",
            "BIGTIFF=IF_SAFER",
            "NUM_THREADS=ALL_CPUS",
        ]
        if method == "ZSTD":
            options.append("ZSTD_LEVEL=9")
        gdal.Translate(str(tmp), src, creationOptions=options)
        src = None

        ok, why = same_pixels(path, tmp)
        if not ok:
            tmp.unlink(missing_ok=True)
            row.update(status="check_failed", detail=why)
            return row

        new_bytes = tmp.stat().st_size
        os.replace(tmp, path)  # atomic rename on the same drive
        row.update(status="compressed", new_bytes=new_bytes, detail=f"was {comp}")
        return row
    except Exception as e:  # unreadable or truncated file, disk error, etc.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        row.update(status="error", detail=str(e).replace("\n", " ")[:300])
        return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="Folder holding the huc6_* folders")
    ap.add_argument("--method", choices=["ZSTD", "DEFLATE"], default="ZSTD")
    ap.add_argument("--workers", type=int, default=3, help="Files processed at once [default: 3]")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N files")
    ap.add_argument("--dry-run", action="store_true", help="Report what would be done; change nothing")
    ap.add_argument("--log", type=Path, default=None, help="CSV log path [default: <root>/compress_log_<time>.csv]")
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    files = find_dems(root)
    if args.limit:
        files = files[: args.limit]
    log_path = args.log or root / f"compress_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
    print(f"{len(files)} DEM files under {root}")
    print(f"Method {args.method}, {args.workers} workers, {'DRY RUN' if args.dry_run else 'writing'}")
    print(f"Log: {log_path}\n")

    counts: dict[str, int] = {}
    saved = 0
    start = time.time()
    with open(log_path, "w", newline="") as f, ProcessPoolExecutor(max_workers=args.workers) as ex:
        writer = csv.DictWriter(f, fieldnames=["path", "status", "old_bytes", "new_bytes", "detail"])
        writer.writeheader()
        futures = [ex.submit(compress_one, str(p), args.method, args.dry_run) for p in files]
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            writer.writerow(row)
            f.flush()
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if row["status"] == "compressed":
                saved += int(row["old_bytes"]) - int(row["new_bytes"])
            if row["status"] in ("error", "check_failed"):
                print(f"  {row['status']}: {row['path']}  ({row['detail']})")
            if i % 50 == 0 or i == len(files):
                mins = (time.time() - start) / 60
                print(f"[{i}/{len(files)}] {mins:.1f} min, {saved / 1e9:.1f} GB saved so far, {counts}")

    print(f"\nDone. {counts}. Saved {saved / 1e9:.1f} GB. Log: {log_path}")


if __name__ == "__main__":
    main()
