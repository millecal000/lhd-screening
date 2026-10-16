"""Rename generic LHD dam entries from OSM-named dams/weirs within RADIUS_M.

Adds (or refreshes) an `OSM_Name` column on full_lhd_website.csv.
Queries Overpass in ~3deg tiles and caches each tile to disk so a kill/restart resumes.
"""
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'data' / 'full_lhd_website.csv'
LOG_CSV = ROOT / 'scripts' / 'osm_rename_log.csv'
PROGRESS = ROOT / 'scripts' / 'osm_rename_progress.log'
CACHE_DIR = ROOT / 'cache' / 'osm_dams'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

RADIUS_M = 50.0
TILE_DEG = 3.0
OVERPASS_MIRRORS = [
    'https://overpass.private.coffee/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
    'https://overpass-api.de/api/interpreter',
]
UA = 'lhd-screening-rename/1.0 (contact: kennethtquintana@gmail.com)'

generic_pat = re.compile(
    r'^(wade unspecified|low-?head dam( \(\d+\))?|lhd|added-?\d*|control structure( ?#?\d+)?|dam|unnamed|n/?a)$',
    re.I,
)


def is_generic(nm: str) -> bool:
    if not nm:
        return True
    return bool(generic_pat.match(nm.strip()))


def hav(a, b, c, d):
    R = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp = math.radians(c - a)
    dl = math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(PROGRESS, 'a') as f:
        f.write(line + '\n')


def fetch_overpass(s, w, n, e, attempts_per_mirror=2):
    q = (
        f'[out:json][timeout:60];'
        f'(way["name"]["waterway"~"^(dam|weir)$"]({s},{w},{n},{e});'
        f'way["name"]["man_made"="dam"]({s},{w},{n},{e});'
        f'node["name"]["waterway"~"^(dam|weir)$"]({s},{w},{n},{e});'
        f'node["name"]["man_made"="dam"]({s},{w},{n},{e}););'
        f'out tags center;'
    )
    body = urllib.parse.urlencode({'data': q}).encode()
    for mirror in OVERPASS_MIRRORS:
        for k in range(attempts_per_mirror):
            try:
                req = urllib.request.Request(
                    mirror, data=body,
                    headers={'User-Agent': UA, 'Accept': 'application/json'},
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                log(f'   {mirror.split("/")[2]} try {k + 1}: {e}')
                time.sleep(3)
        log(f'   mirror exhausted, falling through to next')
    return {'elements': []}


def main():
    PROGRESS.write_text('')
    log(f'reading {SRC}')
    with open(SRC, newline='') as f:
        rows = list(csv.reader(f))
    header = rows[0]
    data = rows[1:]
    i_oid = header.index('OBJECTID')
    i_la = header.index('Latitude')
    i_lo = header.index('Longitude')
    i_nm = header.index('Dam_Name')

    targets = []   # (row_idx, lat, lon)
    for ridx, row in enumerate(data):
        if not is_generic(row[i_nm]):
            continue
        try:
            la = float(row[i_la])
            lo = float(row[i_lo])
        except (ValueError, IndexError):
            continue
        targets.append((ridx, la, lo))
    log(f'generic-named target dams with coords: {len(targets)}')

    # bucket by TILE_DEG
    tiles = defaultdict(list)
    for t in targets:
        _, la, lo = t
        key = (math.floor(la / TILE_DEG), math.floor(lo / TILE_DEG))
        tiles[key].append(t)
    log(f'tiles to query: {len(tiles)}')

    osm_elems = []   # (name, lat, lon)
    tile_keys = sorted(tiles.keys())
    for k, key in enumerate(tile_keys, 1):
        ty, tx = key
        s = ty * TILE_DEG
        n = s + TILE_DEG
        w = tx * TILE_DEG
        e = w + TILE_DEG
        cache_file = CACHE_DIR / f'tile_{ty}_{tx}.json'
        if cache_file.exists():
            with open(cache_file) as f:
                d = json.load(f)
            cached = True
        else:
            d = fetch_overpass(s, w, n, e)
            with open(cache_file, 'w') as f:
                json.dump(d, f)
            cached = False
        count_named = 0
        for el in d.get('elements', []):
            tags = el.get('tags', {})
            nm = tags.get('name')
            if not nm:
                continue
            if el.get('type') == 'node':
                la = el.get('lat')
                lo = el.get('lon')
            else:
                c = el.get('center') or {}
                la = c.get('lat')
                lo = c.get('lon')
            if la is None or lo is None:
                continue
            osm_elems.append((nm, la, lo))
            count_named += 1
        log(
            f'[{k:>3}/{len(tile_keys)}] tile ({ty},{tx}) bbox '
            f'{s:.0f},{w:.0f}..{n:.0f},{e:.0f}  '
            f'targets={len(tiles[key])}  named_osm={count_named}'
            + ('  (cached)' if cached else '')
        )
        if not cached:
            time.sleep(0.3)

    log(f'total OSM named features collected: {len(osm_elems)}')

    # spatial join, 100m bucket
    cell = 0.001
    bk = defaultdict(list)
    for idx, (nm, la, lo) in enumerate(osm_elems):
        bk[(round(la / cell), round(lo / cell))].append(idx)

    osm_name_per_row = {}
    rename_rows = []
    for ridx, la, lo in targets:
        cy = round(la / cell)
        cx = round(lo / cell)
        best = (None, 1e9)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for idx in bk.get((cy + dy, cx + dx), []):
                    nm, ola, olo = osm_elems[idx]
                    dist = hav(la, lo, ola, olo)
                    if dist < best[1]:
                        best = (nm, dist)
        if best[0] is not None and best[1] <= RADIUS_M:
            osm_name_per_row[ridx] = best[0]
            rename_rows.append((data[ridx][i_oid], data[ridx][i_nm], best[0], f'{best[1]:.1f}', la, lo))

    log(f'matched OSM names for {len(osm_name_per_row)} of {len(targets)} targets')

    if 'OSM_Name' in header:
        col = header.index('OSM_Name')
        for i, row in enumerate(data):
            if i in osm_name_per_row:
                row[col] = osm_name_per_row[i]
            elif len(row) > col:
                pass
    else:
        header.append('OSM_Name')
        for i, row in enumerate(data):
            row.append(osm_name_per_row.get(i, ''))

    with open(SRC, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(data)
    log(f'wrote OSM_Name column to {SRC}')

    with open(LOG_CSV, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['OBJECTID', 'old_dam_name', 'osm_name', 'distance_m', 'Latitude', 'Longitude'])
        w.writerows(rename_rows)
    log(f'wrote rename log to {LOG_CSV} ({len(rename_rows)} rows)')


if __name__ == '__main__':
    sys.exit(main())
