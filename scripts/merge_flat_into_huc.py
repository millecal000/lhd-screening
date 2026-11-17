"""One-time migration: move per-dam subdirs from the flat staging dir
(DEM/STRM/LAND/FLOW/RESULTS/<dam_id>/) into the HUC-batched layout
(huc<level>_<KEY>/<sub>/<dam_id>/) under the new staging root.

For each <sub>/<dam_id>/ in the flat dir:
  - look up HUC8 in the master dam table, derive the batch key at --huc-level
  - move the dir into <new-root>/huc<level>_<key>/<sub>/<dam_id>/
  - if the target slot is already a symlink (the existing-data-dir flow),
    unlink first, then move
  - if it's already a real dir, skip (don't overwrite)
  - if the bundle dir doesn't exist yet, skip (that HUC hasn't been run)

After all moves, scrub external_links entries pointing at the flat dir
from huc8_ledger.json. Dry-run by default; pass --apply to actually move.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

# Make backend/ importable so this script uses the shared dam-table module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table  # noqa: E402

_REUSE_SUBDIRS = ("DEM", "STRM", "LAND", "FLOW", "RESULTS")


def _huc_dirname(key: str) -> str:
    return f"huc{len(key)}_{key}"


def _load_dam_to_key(dams_table: Path, huc_level: int) -> Dict[int, str]:
    df = read_dam_table(dams_table)
    if "HUC8" not in df.columns:
        sys.exit(f"{dams_table} has no HUC8 column. Run backend/assign_huc8.py first.")
    df = df.dropna(subset=["OBJECTID", "HUC8"])
    df["OBJECTID"] = df["OBJECTID"].astype(int)
    df["HUC8"] = df["HUC8"].astype(str).str.split(".").str[0].str.zfill(8)
    df = df[df["HUC8"] != "00000000"]
    return {
        int(r.OBJECTID): str(r.HUC8)[:huc_level]
        for r in df.itertuples(index=False)
    }


def _plan_moves(
    flat_dir: Path,
    new_root: Path,
    dam_to_key: Dict[int, str],
) -> Tuple[List[Tuple[Path, Path]], Dict[str, int]]:
    """Return (moves, skip_counts).

    Each move is (src, dst) where dst's parent (the bundle's <sub>/) already
    exists. Skipped categories tracked in skip_counts.
    """
    moves: List[Tuple[Path, Path]] = []
    skip: Dict[str, int] = {
        "no_huc_mapping": 0,
        "bundle_missing": 0,
        "target_is_real_dir": 0,
        "target_symlink_other": 0,
        "not_a_dir": 0,
    }
    for sub in _REUSE_SUBDIRS:
        src_sub = flat_dir / sub
        if not src_sub.is_dir():
            continue
        for entry in src_sub.iterdir():
            try:
                dam_id = int(entry.name)
            except ValueError:
                continue
            if not entry.is_dir() or entry.is_symlink():
                skip["not_a_dir"] += 1
                continue
            key = dam_to_key.get(dam_id)
            if not key:
                skip["no_huc_mapping"] += 1
                continue
            bundle = new_root / _huc_dirname(key)
            if not bundle.is_dir():
                skip["bundle_missing"] += 1
                continue
            target = bundle / sub / str(dam_id)
            if target.is_symlink():
                try:
                    resolved = target.resolve()
                except OSError:
                    resolved = None
                # symlink points back at this source → ok to replace.
                # symlink points elsewhere → don't touch; another existing
                # dir already filled this slot.
                if resolved != entry.resolve():
                    skip["target_symlink_other"] += 1
                    continue
            elif target.exists():
                skip["target_is_real_dir"] += 1
                continue
            moves.append((entry, target))
    return moves, skip


def _apply_moves(moves: List[Tuple[Path, Path]]) -> Tuple[int, List[Tuple[Path, str]]]:
    moved = 0
    errors: List[Tuple[Path, str]] = []
    for src, dst in moves:
        try:
            if dst.is_symlink():
                dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except (OSError, shutil.Error) as e:
            errors.append((src, str(e)))
    return moved, errors


def _scrub_ledger(new_root: Path, flat_dir: Path) -> int:
    """Drop external_links entries whose paths sit under flat_dir."""
    ledger_path = new_root / "huc8_ledger.json"
    if not ledger_path.exists():
        return 0
    with open(ledger_path) as f:
        ledger = json.load(f)
    flat_abs = str(flat_dir.resolve())
    scrubbed = 0
    for entry in ledger.values():
        ext = entry.get("external_links")
        if not ext:
            continue
        for did, sub_map in list(ext.items()):
            for sub, src in list(sub_map.items()):
                if src.startswith(flat_abs):
                    sub_map.pop(sub)
                    scrubbed += 1
            if not sub_map:
                ext.pop(did)
        if not ext:
            entry.pop("external_links", None)
            entry.pop("uses_external_data", None)
            entry.pop("external_paths", None)
    tmp = ledger_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    tmp.replace(ledger_path)
    return scrubbed


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--flat-dir", type=Path, required=True,
                   help="Old staging dir with DEM/STRM/LAND/FLOW/RESULTS subdirs")
    p.add_argument("--new-root", type=Path, required=True,
                   help="New staging root containing huc<level>_<KEY>/ bundles")
    # --dams-csv is kept as an old alias of --dams-table.
    p.add_argument("--dams-table", "--dams-csv", dest="dams_table", type=Path,
                   default=DEFAULT_DAM_TABLE)
    p.add_argument("--huc-level", type=int, choices=[2, 4, 6, 8], default=6)
    p.add_argument("--apply", action="store_true",
                   help="Actually move files. Without this, prints a dry-run plan.")
    args = p.parse_args()

    if not args.flat_dir.is_dir():
        sys.exit(f"--flat-dir not found: {args.flat_dir}")
    if not args.new_root.is_dir():
        sys.exit(f"--new-root not found: {args.new_root}")
    if not args.dams_table.exists():
        sys.exit(f"--dams-table not found: {args.dams_table}")

    dam_to_key = _load_dam_to_key(args.dams_table, args.huc_level)
    moves, skip = _plan_moves(args.flat_dir, args.new_root, dam_to_key)

    by_bundle: Dict[str, int] = {}
    for _, dst in moves:
        b = dst.parents[1].name
        by_bundle[b] = by_bundle.get(b, 0) + 1

    print(f"Flat dir:  {args.flat_dir}")
    print(f"New root:  {args.new_root}")
    print(f"HUC level: {args.huc_level}")
    print(f"\nPlanned moves: {len(moves)}")
    for b in sorted(by_bundle):
        print(f"  {b}: {by_bundle[b]}")
    print(f"\nSkipped:")
    for k, n in skip.items():
        if n:
            print(f"  {k}: {n}")

    if not args.apply:
        print("\n(dry run — rerun with --apply to move)")
        return

    print("\nMoving …")
    moved, errors = _apply_moves(moves)
    print(f"Moved {moved} dir(s).")
    if errors:
        print(f"! {len(errors)} error(s):")
        for src, msg in errors[:20]:
            print(f"  {src}: {msg}")

    scrubbed = _scrub_ledger(args.new_root, args.flat_dir)
    if scrubbed:
        print(f"Cleared {scrubbed} stale external_links entry(ies) from ledger.")

    print(f"\nDone. You can now drop --existing-data-dir from rolling_pipeline.py "
          f"calls. When the flat dir is empty, `rm -rf {args.flat_dir}`.")


if __name__ == "__main__":
    main()
