"""
build_nwm_fdc.py — build frontend/data/nwm_fdc.json from NWM API v2.

Fetches pre-computed NWM Retrospective v3.0 flow-duration-curve percentiles
for every unique Reach_ID (ComID) in the master CSV, via the CIROH
/percentile-flows endpoint.  Writes a compact JSON file that the frontend
loads once and uses to render site-specific FDC panels.

Output schema
-------------
{
  "_meta": {
    "percentiles": [0, 2, 5, 10, 20, 25, 30, 50, 75, 90, 95, 99, 100],
    "units": "cms",
    "source": "NWM Retrospective v3.0 via CIROH NWM API v2"
  },
  "<comid>": [flow_p0, flow_p2, ..., flow_p100],   # 13 values in cms
  ...
}

Exceedance probability for value i = 100 - percentiles[i].

Usage
-----
    python backend/build_nwm_fdc.py
    python backend/build_nwm_fdc.py --batch-size 300 --out frontend/data/nwm_fdc.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Master dam table (Parquet) reader shared by every script.
from lhd_processor.dam_table import DEFAULT_DAM_TABLE, read_dam_table
import requests

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT    = _BACKEND_ROOT.parent

DEFAULT_TABLE = DEFAULT_DAM_TABLE  # data/full_lhd_website.parquet
DEFAULT_OUT = _REPO_ROOT / "frontend" / "data" / "nwm_fdc.json"
NWM_API_BASE = "https://nwm-api-v2-9f6idmxh.uc.gateway.dev"

PERCENTILE_LEVELS = [0, 2, 5, 10, 20, 25, 30, 50, 75, 90, 95, 99, 100]
PCT_STR = ",".join(str(p) for p in PERCENTILE_LEVELS)

# API field name for each percentile level
def _api_field(p: int) -> str:
    if p == 0:   return "min"
    if p == 100: return "max"
    return f"perc_{p}"

API_FIELDS = [_api_field(p) for p in PERCENTILE_LEVELS]


def _load_api_key() -> str:
    key = os.environ.get("NWM_API_KEY", "")
    if key:
        return key
    env_path = _REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("NWM_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    return ""


def _fetch_batch(comids: list[int], api_key: str, retries: int = 3) -> dict[str, list[float]]:
    """Fetch one batch of ComIDs. Returns {str(comid): [13 cms values]}."""
    reach_str = ",".join(str(c) for c in comids)
    for attempt in range(retries):
        try:
            resp = requests.get(
                f"{NWM_API_BASE}/percentile-flows",
                params={"reach_id": reach_str, "percentiles": PCT_STR,
                        "output_format": "json", "key": api_key},
                timeout=120,
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"  retry {attempt+1}/{retries} after {wait}s — {e}", flush=True)
                time.sleep(wait)
            else:
                print(f"  batch failed after {retries} attempts: {e}", flush=True)
                return {}

    records = payload if isinstance(payload, list) else payload.get("features", [])
    result: dict[str, list[float]] = {}
    for rec in records:
        props = rec.get("properties", rec)
        cid = props.get("reach_id")
        if cid is None:
            continue
        flows = []
        for field in API_FIELDS:
            v = props.get(field)
            flows.append(float(v) if v is not None else None)
        if any(v is not None for v in flows):
            result[str(int(cid))] = flows
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # --csv is kept as an old alias of --table so existing commands still parse.
    ap.add_argument("--table", "--csv", dest="table", type=Path, default=DEFAULT_TABLE,
                    help="Master dam table (.parquet) [default: data/full_lhd_website.parquet]")
    ap.add_argument("--out",        type=Path, default=DEFAULT_OUT)
    ap.add_argument("--split-dir",  type=Path,
                    default=DEFAULT_OUT.parent / "fdc",
                    help="Also write one JSON file per ComID here (default: frontend/data/fdc/)")
    ap.add_argument("--batch-size", type=int,  default=200,
                    help="ComIDs per API request (default: 200)")
    ap.add_argument("--resume",     action="store_true",
                    help="Load existing output and only fetch missing ComIDs")
    args = ap.parse_args()

    api_key = _load_api_key()
    if not api_key:
        print("ERROR: NWM_API_KEY not set. Add it to .env or environment.", file=sys.stderr)
        return 1

    print(f"Loading {args.table} …", flush=True)
    df = read_dam_table(args.table, columns=["Reach_ID"])  # only Reach_ID is used
    comids = (
        pd.to_numeric(df["Reach_ID"], errors="coerce")
        .dropna().astype(int)
        .unique().tolist()
    )
    print(f"Unique ComIDs: {len(comids):,}", flush=True)

    # Resume: load existing file and skip already-fetched ComIDs
    existing: dict = {}
    if args.resume and args.out.exists():
        with open(args.out) as f:
            existing = json.load(f)
        existing.pop("_meta", None)
        already = len(existing)
        comids = [c for c in comids if str(c) not in existing]
        print(f"Resuming: {already:,} already cached, {len(comids):,} remaining", flush=True)

    batches = [comids[i:i+args.batch_size] for i in range(0, len(comids), args.batch_size)]
    print(f"Fetching {len(comids):,} ComIDs in {len(batches)} batches …", flush=True)

    result = dict(existing)
    t0 = time.time()
    for i, batch in enumerate(batches, 1):
        t_batch = time.time()
        fetched = _fetch_batch(batch, api_key)
        result.update(fetched)
        elapsed = time.time() - t_batch
        total_done = len(existing) + (i * args.batch_size)
        print(f"  batch {i}/{len(batches)}: {len(fetched)}/{len(batch)} ok "
              f"({elapsed:.1f}s) — {len(result):,} total", flush=True)

        # Write incrementally every 10 batches (monolithic only; split written at end)
        if i % 10 == 0 or i == len(batches):
            _write(result, args.out)

    _write(result, args.out, args.split_dir)
    print(f"\nDone in {time.time()-t0:.0f}s — {len(result):,} ComIDs → {args.out}", flush=True)
    return 0


def _write(result: dict, out_path: Path, split_dir: Path | None = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "percentiles": PERCENTILE_LEVELS,
            "units": "cms",
            "source": "NWM Retrospective v3.0 via CIROH NWM API v2",
        }
    }
    payload.update(result)
    with open(out_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    size_kb = out_path.stat().st_size / 1024
    print(f"  → wrote {out_path} ({size_kb:.0f} KB)", flush=True)

    if split_dir:
        split_dir.mkdir(parents=True, exist_ok=True)
        for comid, flows in result.items():
            with open(split_dir / f"{comid}.json", "w") as f:
                json.dump(flows, f, separators=(",", ":"))
        print(f"  → wrote {len(result)} per-COMID files to {split_dir}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
