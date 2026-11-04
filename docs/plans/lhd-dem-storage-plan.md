# LHD DEM Storage Plan: Local Compression vs. Direct S3 COG Reads

Nov 4, 2026 · Caleb Miller

> Snapshot of the live Claude Doc: https://claude.ai/artifact/C1Lc3KpYEfrVzLwZT6WsEs. The doc may have changed since this copy was made.

## Summary

Run Option 1 now to get one working instance on the lab Mac, and prototype Option 2 on a single HUC alongside it. Option 1 fixes today's storage problem in days; Option 2 removes the need to store DEMs at all, but depends on USGS's servers and needs a code change and a test before anyone relies on it.

The problem: the external drive (EB406_T7_2) holds 15,916 per-dam DEM clips, 600 GB across 315 HUC6 watersheds. The lab Mac has 52 GB free, so the pipeline can't copy them locally.

- **Option 1, compress and keep local.** Recompress the existing clips in place on the external drive (about 600 GB to about 220 GB), treat the drive as read-only, and run the pipeline on the Mac with outputs on its internal disk.
- **Option 2, read from USGS directly.** Skip the downloaded tiles and stored clips. For each dam, read only its rectangle straight from USGS's public 3DEP storage on Amazon S3.

## Where the DEMs come from today

Every `dem_{dam_id}.tif` is a clip of public USGS 3DEP elevation data, written by the `lhd-screening` pipeline in three steps:

1. `backend/stage_nhd_dem.py` takes each dam's NHD flowline (1 km upstream, 1.5 km downstream) and asks The National Map API for 3DEP tiles covering it: 1 m first, then 1/9 arc-second (about 3 m), then 1/3 arc-second (about 10 m). It downloads each unique tile once to `DEM/raw_3dep/` and records dam-to-tile URLs in `tile_manifest.json`.
2. `backend/build_trimmed_dems.py` buffers the flowline by 500 m, merges the dam's tiles, and writes the clip.
3. `run_wsp_pipeline.py` deletes `raw_3dep/` by default once clips exist, so the clips are the only local copy.

Three problems come from how step 2 writes the files (`_mosaic_and_clip`):

- **Weak compression.** It sets `compress="lzw"` with no predictor and strips the tiling. On a test file (`dem_9034.tif`, Float32), recompressing with ZSTD, predictor 3 and 512 px tiles cut 71 MB to 26 MB with identical min and max values.
- **Four files likely truncated.** Four clips in `huc6_160503` (9276, 9544, 9655, 10460) are exactly 4.0 GB, the classic TIFF limit. Rasterio can't predict a compressed file's size, so it doesn't switch to BigTIFF. They need checking and probably rebuilding.
- **Duplicated terrain.** Neighbouring dams share tiles, but each clip stores its own copy of the overlap.

Rebuilding a clip is possible but not exactly reproducible: 3DEP keeps adding newer lidar, so a rerun may pick a different tile than the original.

## Option 1: compress on the external drive, run locally

Recompressing the clips in place should shrink the drive's DEMs from about 600 GB to about 220 GB, without changing any pixel values. The pipeline then reads DEMs from the drive and writes outputs to the Mac.

**Steps**

1. Confirm the backup is complete: spot-check that a few files from different HUCs open.
2. Check the four 4.0 GB files with `gdalinfo`. Rebuild any that fail, or set them aside.
3. Fix `_mosaic_and_clip` so new clips are written small: `compress="zstd"`, `predictor=3`, `tiled=True` with 512×512 blocks, `BIGTIFF="IF_SAFER"`. Test that the pipeline's `uv` environment (rasterio's bundled GDAL) opens a ZSTD file; if not, use `"deflate"`.
4. Convert the existing clips in place with a script that, for each file: writes a compressed copy beside it, confirms the pixels match (`gdalcompare.py`), then renames the copy over the original. It logs progress and skips finished files, so it can be stopped and resumed.
5. Remount the drive read-only (`diskutil mount readOnly`) so nothing writes to it by accident.
6. Point the pipeline's DEM input at the drive and its outputs at the Mac. Run one medium HUC, measure output size and runtime, then scale up.

**Expected cost**

- Time: about 2.5 hours just to read 600 GB at the measured 75 MB/s, plus writing about 220 GB and CPU time. Plan for an overnight run with `caffeinate` keeping the Mac awake.
- Space: about 380 GB freed on the drive. Outputs fit on the Mac if they're mostly rating-curve CSVs; derived rasters per dam would need cleanup or a nightly copy back to the drive.

**Risks**

- The drive appears to be exFAT, which has no journaling, so an interrupted write can corrupt a file. The write-then-rename step limits that to one temporary file.
- ZSTD isn't readable by older tools such as some ArcGIS versions. DEFLATE avoids that at a slightly larger size.
- The data stays tied to one physical drive and one Mac.

## Option 2: read 3DEP directly from USGS on S3

The pipeline already knows exactly which USGS file each dam needs, so it could read just each dam's rectangle over the internet instead of downloading whole tiles and storing clips.

**What the existing manifests show**

The 317 `tile_manifest.json` files on the external drive list 17,185 tile references, all on USGS's public bucket `prd-tnm.s3.amazonaws.com`:

| Source | Tile references | Format | Can be read in pieces |
| --- | --- | --- | --- |
| 3DEP 1 m (`Elevation/1m/Projects/…/TIFF/`) | 16,753 (97.5%) | GeoTIFF | Yes if COG (see open questions) |
| 1/9 arc-second (`Elevation/19/IMG/`) | 349 (2.0%) | Zipped IMG | No: the whole zip must be downloaded |
| 1/3 arc-second (`Elevation/13/TIFF/`) | 83 (0.5%) | GeoTIFF | Yes if COG |

Web lookups and direct requests to USGS were blocked from this session, so whether the 1 m tiles are COGs is not yet confirmed. If they are, GDAL fetches only the 512-pixel blocks inside a dam's rectangle; if not, it still works but reads far more data per dam.

**How it would work**

1. Keep `stage_nhd_dem.py`'s flowline and tile lookup, but skip the download step (`--skip-download` already exists).
2. In `build_trimmed_dems.py`, open each tile as `/vsicurl/<url>` instead of a local path. `rasterio.merge` with `bounds=` then reads only the dam's window. The 1/9 arc-second zips stay on the download path.
3. Write the clip as a small, properly compressed local file (Option 1's settings), or delete it once that dam's outputs are written.
4. Set GDAL's network options once in a `rasterio.Env`: `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`, `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif`, `GDAL_HTTP_MULTIRANGE=YES`, `GDAL_HTTP_MAX_RETRY=5` and `GDAL_HTTP_RETRY_DELAY=2`.
5. Record each tile's URL plus its `Last-Modified` or ETag in the manifest, so a rerun can tell when USGS has replaced a tile.

**What it costs**

- Storage: close to zero for DEMs; no 600 GB copy anywhere.
- Network: each full run downloads roughly what the clips hold, on the order of 220 GB compressed. Speed will depend on request latency more than bandwidth, so parallel workers matter.
- Code: a few dozen lines in `build_trimmed_dems.py`, plus a flag to choose local or remote reads.

**Risks**

- Every run depends on USGS's bucket being up and reachable. The lab Mac's network has not been tested against it yet.
- USGS can replace or remove tiles, so a dam's elevation can change between runs unless results record which file version they used.
- If the website or later steps reopen the DEMs, those steps need the same remote reads or a local cache.

## Side by side

Option 1 is faster to a working instance; Option 2 is the better long-term shape.

| | Option 1: compress, run locally | Option 2: read from USGS S3 |
| --- | --- | --- |
| DEM storage | About 220 GB on the external drive | Near zero; optional small local cache |
| Network per full run | None | Roughly 220 GB, latency-bound |
| Code changes | Compression settings in `_mosaic_and_clip` + a conversion script | Remote reads in `build_trimmed_dems.py` + GDAL network settings |
| Time to first working run | About 1 to 2 days, including an overnight conversion | Several days, including a test on one HUC |
| Reproducibility | Fixed: the clips stay as they were built | Changes when USGS updates tiles, unless versions are recorded |
| Depends on | One drive and one Mac | USGS's bucket and the lab network |
| Fits "point the site at a data location" | Partly: a fixed path on one machine | Yes: a URL works from any machine or cloud |

The time estimates are rough and assume no surprises in the four oversized files or the network.

## What we learned after the first draft

Almost all dams already have DEMs, and the pipeline reuses them automatically, so a new run needs to download very little.

**Coverage on the drive** (from `data/full_lhd_website.csv`, excluding dams reviewed as not LHDs)

- 15,692 eligible dams fall in 306 HUC6 groups. 15,305 of them (97.5%) already have a trimmed DEM on the drive.
- Only 387 dams in 25 HUC6s still need tiles downloaded. The largest gaps are `101900` (98 dams), `160502` (64), `102500` (43) and `170402` (36).
- 9,318 dams have a `wsp_result.json` on the drive, 7,319 with status `ok`. The WSP and danger-range columns in the current CSV are empty after the October reset.
- 24 dams in the CSV have no HUC6 folder: 10 have no HUC assigned (`000000`) and 14 are in four HUC6s never downloaded.

**What the pipeline reuses** (`run_wsp_pipeline.py`)

- HUCs marked `ready_to_archive` or `archived` in `wsp_ledger.json` are skipped. The ledger lives in `--local-staging-root`, so a new root starts with every HUC pending.
- Dams with an existing `wsp_result.json` in that root are skipped.
- Dams with an existing `dem_{id}.tif` skip downloading. `--existing-data-dir` links DEM and flowline folders from another tree, such as the drive, as symlinks instead of copying them.
- Raw 3DEP tiles are deleted after each HUC, so at most one HUC's tiles exist at a time.
- Every run ends by rewriting `data/full_lhd_website.csv` and `frontend/data/full_lhd_website.csv`.

**Validating a rerun**

The synthetic rating curves come from Lynker hydrofabric coefficients, not from DEMs, so comparing them can't confirm the DEM-dependent results. Check these instead, cheapest first:

1. Tile lists in the old and new `tile_manifest.json`: split dams into same-tile and changed-tile groups.
2. Pixel comparison of old and new clips for same-tile dams (`gdalcompare.py`).
3. Old status against new status for every dam (`ok`, `negative_delta_wse`, `no_dem` and so on).
4. Height, Δwse and crest length for dams `ok` in both runs, with a tolerance for changed-tile dams.
5. `Qmin_env` and `Qmax_env` against the 10,690 values in `full_lhd_website.archivedkenney.csv`.
6. Agreement of WSP height with NID heights (`NID_Height_Ft`), old run against new.
7. Plots of the 20 largest differences.

Reusing the old DEMs first (Run A) isolates code changes; a later fully fresh run (Run B) then isolates data changes.

## Plan: run the pipeline on the lab Mac with uv

Run with a staging folder on the Mac that links to the DEMs on the external drive, so only results and about 387 dams' new DEMs are written locally. Run the commands from the repo folder (`cd ~/git/lhd-screening`) in Terminal.

1. **Check uv and the Mac's macOS version.** Run `uv --version` and `sw_vers -productVersion`. If uv is missing, install it with `brew install uv`. The rasterio wheel locked for Intel Macs targets macOS 15, so an older macOS may fail at the next step.
2. **Build the environment.** `uv sync` reads `uv.lock` and creates `.venv` in the repo. Then check it: `uv run python -c "import rasterio, geopandas, pynhd; print(rasterio.__gdal_version__)"`.
3. **Protect the CSVs.** Every run rewrites `data/full_lhd_website.csv` and `frontend/data/full_lhd_website.csv`, and the data CSV already has an uncommitted change. Commit that change or copy both files somewhere safe before the first run.
4. **Test one HUC with no downloads.** `caffeinate -i uv run python backend/run_wsp_pipeline.py --local-staging-root ~/lhd_staging --existing-data-dir /Volumes/EB406_T7_2/lhd-nwm --huc 030300 --min-free-gb 20 --workers 4`
   - `caffeinate -i`: keeps the Mac awake while the command runs.
   - `--local-staging-root ~/lhd_staging`: the existing empty folder on the Mac; the new ledger, links and results go here.
   - `--existing-data-dir /Volumes/EB406_T7_2/lhd-nwm`: links in the drive's DEMs and flowlines instead of downloading.
   - `--huc 030300`: one HUC6 with 60 dams, all with DEMs.
   - `--min-free-gb 20`: the default is 200 GB, which would stop the run on a Mac with 52 GB free.
   - The output should say the DEMs were reused via symlinks and that staging was skipped.
5. **Check space and results.** `du -sh ~/lhd_staging` and `df -h ~`, then compare the new `WSP_RESULTS` with the drive's for that HUC (validation steps 3 and 4).
6. **Fix the DEM compression before downloading anything.** The 387 new DEMs will be written to the Mac, so apply the `_mosaic_and_clip` fix first and they come out about 60% smaller.
7. **Run everything.** Repeat step 4 without `--huc`. It works through all 306 HUC6 groups, downloading only for the 25 that need it, and the free-space check stops it before a HUC if the Mac drops below 20 GB. Stop it any time with Ctrl+C and rerun: the ledger picks up where it left off. `--status` shows progress.
8. **Validate, then decide what to keep.** Run the validation checks against the drive's results and the archived CSV, then commit the updated CSVs if they hold up.

The external drive must stay connected for the whole run, because the linked DEMs are read from it. The in-place compression of the drive's DEMs (Option 1) can wait until after this run.

## Recommended sequence

Fix the code once, then let Option 1 carry the first run while Option 2 is tested on a single HUC.

1. Change `_mosaic_and_clip`'s compression settings. Both options need this, and it stops new clips from being written bloated.
2. Check the COG question below. It takes a minute and decides how attractive Option 2 is.
3. Run Option 1's in-place conversion overnight, then the first single-HUC pipeline run from the read-only drive.
4. Build Option 2 behind a flag and run the same HUC both ways. Compare outputs and runtime.
5. If the results match and runtime is acceptable, make Option 2 the default and keep the compressed drive as a backup cache.

## Open questions

- [ ] Are the 3DEP 1 m tiles COGs? On the Mac, in the `gdal` environment, run `gdalinfo /vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/ME_CrownofMaine_2018_A18/TIFF/USGS_1M_19_x54y523_ME_CrownofMaine_2018_A18.tif` and look for `LAYOUT=COG` and a square `Block=` size.
- [ ] Can the lab Mac reach `prd-tnm.s3.amazonaws.com`? The same command answers this.
- [ ] Do any later steps or the website reopen the per-dam DEMs, or only the pipeline outputs?
- [ ] Why do the manifests list 8,196 dams when the drive holds 15,916 DEM files? Manifests may have been overwritten on reruns, or clips may be shared between HUC folders.
- [ ] Are the four 4.0 GB files in `huc6_160503` readable, or truncated?
- [ ] Is the backup complete and readable before the in-place conversion starts?
