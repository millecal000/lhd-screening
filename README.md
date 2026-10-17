# lhd-screening

Public-facing low-head dam (LHD) screening tool. It combines a Leaflet map of
~20k known LHDs with an offline hydraulic screening pipeline built on the
**National Water Model (NWM)** that estimates each dam's crest length, height,
and dangerous-flow range.

- **Frontend** (`frontend/`): static Leaflet map (`index.html` +
  `mapping-logic.js` + `hydraulics.js`) that reads `data/full_lhd_website.csv`.
  Color-codes dams by hazard class, exposes filters (state, owner, hazard,
  HUC), and shows per-reach National Water Model (NWM) flow-duration curves,
  synthetic rating curves, and live NWM forecasts.
- **Backend** (`backend/`): HUC-batched pipeline that stages NHDPlus
  flowlines + 3DEP DEMs, samples a water surface profile along each dam's
  reach, and solves a 1-D energy balance using National Water Model (NWM)
  flows to get dam height and crest length. It then computes each dam's
  dangerous-flow range (Qmin/Qmax).

> **Note on the ARC model:** the old Automated Rating Curve (ARC) pipeline has
> been archived in `backend/arc_archive/` and is no longer used. The `arc`
> package is **not** required anymore.
>
> **Note on naming in the code:** the National Water Model (NWM) pipeline is
> referred to as `wsp` throughout the code (e.g. `run_wsp_pipeline.py`,
> `wsp_ledger.json`, `WSP_RESULTS/`, `Dam_Height_WSP_Ft`). These names are
> intentional and should not be renamed. In this README, the pipeline and its
> results are described as the National Water Model (NWM) pipeline, and file
> and column names are written exactly as they appear in the code. (Inside the
> code, "WSP" literally stands for water surface profile, the sampling method
> the pipeline applies to NWM flows.)

## How the estimate works

For each dam, `run_wsp_pipeline.py`:

1. Uses the dam's NHDPlus V2 `Reach_ID` (COMID). This is the same ID as the
   National Water Model (NWM) `feature_id`, so NWM data keys directly to it.
2. Samples the 3DEP DEM along the dam's reach and the reach just upstream to
   build a water surface profile. The upstream water surface elevation comes
   from the flat pool zone, and the downstream elevation from where the
   profile recovers to the NHD reach slope. The difference is `delta_wse`.
3. Takes **crest length** from the bankfull top width (`owp_tw_bf`) of the
   upstream reach (falling back to the dam reach), from the Lynker
   hydrofabric.
4. Takes the discharge **Q** as the median (p50) flow from the National Water
   Model (NWM) Retrospective v3.0 flow-duration curve (falls back to 1 cms if
   missing).
5. Takes **tailwater depth** from a synthetic rating curve (SRC) built from
   at-a-station hydraulic geometry calibrated to the National Water Model
   (NWM) Retrospective v3.0 (falls back to raw AHG depth).
6. Solves the weir energy balance (`solve_weir_geom`) for dam height `P` and
   head `H`.
7. Computes the dangerous-flow range **Qmin_env / Qmax_env** (cms) from
   the WSP geometry plus SRC tailwater: Qmin is where tailwater crosses the
   conjugate depth, and Qmax is where it crosses the flip depth.

## Setup

The environment is a **micromamba** env (not conda). Create it from
`environment.yml` (conda-forge for the GDAL/rasterio/geopandas binaries, pip
for the rest):

```bash
micromamba create -f environment.yml
micromamba activate lhd-environment
```

Or run any script without activating:

```bash
micromamba run -n lhd-environment python backend/run_wsp_pipeline.py --help
```

`environment.yml` does not list `pyarrow`, `scipy`, or `matplotlib`, which the
backend imports. If you hit an `ImportError`:

```bash
micromamba run -n lhd-environment pip install pyarrow scipy matplotlib
```

`backend/requirements.txt` is a plain-pip alternative to `environment.yml`.

The `backend/lhd_processor/` package lives in this repo; no separate install
is needed.

### Credentials

`backend/build_nwm_fdc.py` pulls National Water Model (NWM) percentile flows
from the CIROH NWM API v2 and needs an API key. Set `NWM_API_KEY` in your
environment, or add this line to a `.env` file at the repo root (it is
gitignored):

```
NWM_API_KEY=your_key_here
```

## Data sources

| Source | Used for |
| --- | --- |
| National Water Model (NWM) Retrospective v3.0 (via CIROH NWM API v2) | Flow-duration-curve percentiles per reach |
| Lynker hydrofabric AHG parquet (`s3://lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet`) | Bankfull top width and depth/width coefficients. Auto-downloaded once (~188 MB) to `backend/cache/` |
| NHDPlus V2 (via `pynhd`) | Flowlines, COMIDs, VAA reach slopes |
| USGS 3DEP (via TNM) | 1 m / 1/9 / 1/3 arc-second DEM tiles |
| USGS WBD | HUC2/4/6/8 assignment (~2.5 GB national GPKG, auto-downloaded to `cache/wbd/`) |
| National Inventory of Dams (NID) | Reference heights and review metadata |

## Backend pipeline

### One-time prep (run in order)

```bash
# 1. HUC codes for every dam (overwrites HUC2/HUC4/HUC6/HUC8 in the master CSV)
python backend/assign_huc8.py

# 2. NHDPlus COMID (Reach_ID) + GNIS river name for every dam
python backend/assign_nhd_comid.py

# 3. National Water Model (NWM) flow-duration curves -> frontend/data/nwm_fdc.json
python backend/build_nwm_fdc.py

# 4. Synthetic rating curves -> frontend/data/synthetic_rating_curves.json
python backend/build_synthetic_rating_curves.py

# 5. Split the two JSON files into per-COMID files the site loads
#    (frontend/data/fdc/<comid>.json and frontend/data/src/<comid>.json)
python backend/split_data_to_files.py
```

Steps 3-5 only need re-running when the set of dam reaches changes.

### Main pipeline: National Water Model (NWM) (`run_wsp_pipeline.py`)

Groups dams by HUC (default HUC6; `--huc-level` accepts 2/4/6/8) and writes
one bundle per group at `<staging-root>/huc6_<KEY>/`. For each batch it:

1. Runs `stage_nhd_dem.py`: NHDPlus flowlines plus the 3DEP tile manifest and
   downloads.
2. Runs `build_trimmed_dems.py`: mosaics and clips a per-dam DEM.
3. Prunes the raw 3DEP tiles (the trimmed DEMs are kept).
4. Runs the in-process NWM-based sampling and energy balance with a thread
   pool.
   Results go to `WSP_RESULTS/<dam_id>/wsp_result.json`.
5. Writes a `.READY_TO_ARCHIVE` marker with counts and sizes.
6. Aggregates results into the master CSV and recomputes the danger range
   (also runs when the disk gate halts the loop).

```bash
python backend/run_wsp_pipeline.py \
    --local-staging-root /path/to/staging \
    --existing-data-dir  /old/lhd_staging \
    --huc-level 6
```

Common flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--local-staging-root` | required | Where HUC bundles and the ledger live |
| `--huc-level {2,4,6,8}` | `6` | Batch granularity |
| `--huc KEY` | all | Process only this group key |
| `--workers N` | `8` | Thread pool size |
| `--min-free-gb N` | `200` | Stop cleanly when free disk drops below this |
| `--search-up M` | `50` | Upstream flat-zone search window in meters |
| `--existing-data-dir P` | none | Reuse DEM/STRM from an earlier staging tree (repeatable) |
| `--reverse` | off | Walk HUC keys from highest to lowest (lets two instances run in parallel) |
| `--dams-csv PATH` | `data/full_lhd_website.csv` | Master dam CSV |

`--existing-data-dir` symlinks already-staged per-dam `DEM/` and `STRM/`
folders (including trees from the archived ARC pipeline) into the new bundle,
so reruns don't refetch DEMs.

Dams with a `Review_Status` of `Removed`, `Confirmed not a LHD`, or `Appears
to not be LHD` are skipped.

A ledger at `<staging-root>/wsp_ledger.json` tracks each batch:
`staging -> wsp_running -> ready_to_archive | partial | errored | archived`.
Reruns skip `ready_to_archive` and `archived`; `partial` and `errored`
batches are retried automatically.

### Master CSV columns written by the pipeline

| Column | Meaning |
| --- | --- |
| `Dam_Height_WSP_Ft` | Estimated dam height `P` (ft) |
| `Dam_Length_WSP_Ft` | Estimated crest length (ft) |
| `WSP_Q_cms` | Discharge used in the energy balance (cms) |
| `WSP_delta_wse_m` | Upstream minus downstream water surface elevation (m) |
| `WSP_upstream_comid` | COMID of the upstream reach used |
| `Qmin_env` / `Qmax_env` | Dangerous-flow range (cms) |

`compute_wsp_danger_range.py` also drops obsolete ARC-only columns
(`Dam_Height_GIS_Ft`, `Dam_Length_GIS_Ft`, `Tailwater_a`, `Tailwater_b`,
`Rp100_cms`, `Qmin_stable`, `Qmax_stable`). It is incremental: dams that
already have `Qmin_env` are skipped. It writes to both `data/` and
`frontend/data/` copies of the CSV.

To refresh manually:

```bash
python backend/run_wsp_pipeline.py --local-staging-root /path/to/staging --aggregate
python backend/compute_wsp_danger_range.py            # add --dry-run to preview
```

### Disk gate and archiving

When free disk drops below `--min-free-gb`, the loop halts cleanly. To clear
the queue in one shot:

```bash
python backend/run_wsp_pipeline.py \
    --local-staging-root /path/to/staging \
    --archive-to /Volumes/ExternalDrive
```

`--archive-to` consolidates symlinks into real files, prunes raw tiles, moves
every `ready_to_archive` bundle to the destination, and flips its ledger status
to `archived`. Add `--include-partial` to move `partial` bundles too.

### Other commands

```bash
# inspect the ledger
python backend/run_wsp_pipeline.py --local-staging-root /path --status

# find the batch a specific dam belongs to
python backend/run_wsp_pipeline.py --local-staging-root /path --locate 1234

# per-key manual archive (rare; --archive-to handles the queue)
python backend/run_wsp_pipeline.py --local-staging-root /path --consolidate 140600
python backend/run_wsp_pipeline.py --local-staging-root /path --mark-archived 140600

# reclaim disk by deleting raw 3DEP tiles from every bundle
python backend/run_wsp_pipeline.py --local-staging-root /path --prune-raw-all

# move archived bundles back (single key, comma list, or "all")
python backend/run_wsp_pipeline.py --local-staging-root /path --restore 140600
```

### Failures and diagnostics

Every dam gets a `WSP_RESULTS/<dam_id>/wsp_result.json`, including failures,
so a failed dam is not retried on every rerun. The `status` field is `ok` or
one of:

`no_comid`, `no_flowline`, `no_comid_col`, `comid_not_in_gpkg`, `no_dem`,
`profile_error:<msg>`, `insufficient_dem_coverage`, `negative_delta_wse`,
`no_tw_bf`, `no_solution`, `exception:<msg>`.

```bash
# summarize all failures into a CSV
python backend/diagnose_wsp_failures.py --staging-root /path/to/staging --failures-only

# preview, relabel, or delete cached failures so they get retried
python backend/reset_wsp_cache.py --staging-root /path/to/staging --dry-run
python backend/reset_wsp_cache.py --staging-root /path/to/staging --relabel api_fail
python backend/reset_wsp_cache.py --staging-root /path/to/staging --delete

# compare WSP height/length against NID reference values
python backend/analyze_wsp_predictions.py
```

### Debugging a single dam

```bash
python backend/test_nhd_wsp.py \
    --dam-id 1234 --lat 40.123 --lon -111.456 --comid 9876543
```

Downloads whatever is missing. Use `--Q` to override the discharge (default is
the National Water Model (NWM) flow-duration-curve p50) and `--search-up` to
widen the upstream search window.

## Frontend

Static site, no build step. The map loads `data/full_lhd_website.csv`,
`data/fdc/<comid>.json`, and `data/src/<comid>.json` by relative URL. Create
the CSV symlink once for local dev (it is gitignored):

```bash
ln -s ../../data/full_lhd_website.csv frontend/data/full_lhd_website.csv
```

On Windows without symlink rights, copy the file to
`frontend/data/full_lhd_website.csv` instead. Then serve it:

```bash
cd frontend && python -m http.server 8000
```

Open <http://localhost:8000>. Dams in Hawaii and Puerto Rico are excluded
because the National Water Model (NWM) does not produce forecasts there.

Live forecasts come from the NOAA National Water Prediction Service API
(`api.water.noaa.gov/nwps/v1/reaches/<comid>/streamflow`).

### Deployment

`.github/workflows/pages.yml` deploys to GitHub Pages on every push to `main`.
It stages `frontend/` plus the repo-root `data/` folder into one site, copies
in `frontend/data/fdc/` and `frontend/data/src/`, and writes `data-version.js`
(the short commit hash) for cache busting.

## Repo layout

```
backend/
  run_wsp_pipeline.py             # HUC-batched orchestrator (main entry point)
  assign_huc8.py                  # one-time WBD HUC assignment
  assign_nhd_comid.py             # one-time COMID + GNIS name assignment
  build_nwm_fdc.py                # National Water Model (NWM) flow-duration curves
  build_synthetic_rating_curves.py# synthetic rating curves (AHG, NWM-calibrated)
  split_data_to_files.py          # explode JSON into per-COMID files for the site
  stage_nhd_dem.py                # NHD flowlines + 3DEP tile staging
  build_trimmed_dems.py           # per-dam trimmed DEMs
  compute_wsp_danger_range.py     # Qmin_env / Qmax_env
  analyze_wsp_predictions.py      # WSP vs NID validation
  diagnose_wsp_failures.py        # failure summary CSV
  reset_wsp_cache.py              # relabel/delete cached failures
  test_nhd_wsp.py                 # single-dam test driver
  screening/                      # WSP sampling + node pickers (wsp.py, etc.)
  lhd_processor/                  # hydraulics (solve_weir_geom) + data download
  arc_archive/                    # archived ARC pipeline (not used)
data/
  full_lhd_website.csv            # master dam CSV (read by site, written by pipeline)
frontend/
  index.html, mapping-logic.js, hydraulics.js, css/
  data/                           # nwm_fdc.json, synthetic_rating_curves.json, fdc/, src/
scripts/                          # data enrichment + validation/paper scripts
cache/                            # WBD GPKG (auto-downloaded, gitignored)
```

Some shared helpers (`backend/screening/height.py`, `width.py`, `reach.py`) and
several comparison scripts in `scripts/` still reference ARC output formats.
They belong to the archived workflow and paper comparisons; the active
pipeline only uses `screening/wsp.py` (plus constants from `reach.py`).

## Known gotchas

- **TNM outages.** `stage_nhd_dem` calls `tnmaccess.nationalmap.gov`. During an
  outage, dams without cached DEMs get no tiles and are cached as `no_dem`.
  Don't start a full sweep during an outage. Afterward, use
  `reset_wsp_cache.py` to clear those cached failures and rerun.
- **Disk space.** Raw 3DEP tiles are large. They are pruned after each batch,
  but keep `--min-free-gb` realistic for your drive.
