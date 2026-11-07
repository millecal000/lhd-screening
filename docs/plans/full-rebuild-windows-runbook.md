# LHD Pipeline: Full Rebuild on Windows (Runbook)

Written 2026-11-06 for the `eb406-win2` Windows PC; updated the same day after the one-watershed rebuild test and the uv DEM fix. Explains each step of rebuilding every HUC-6 from scratch with **uv**, and why each step is there.

## Where things stand

- **Compression is verified.** The archived DEMs on the external drive (`D:\lhd-nwm`) were recompressed losslessly (ZSTD, predictor 3, 512×512 tiles), saving about 348 GB. A 100-dam test across all 18 HUC-2 regions gave identical results with compressed and original-format DEMs (Runs A and B), and again on a second computer (Run D).
- **The July 2026 results can't be reproduced.** Today's pipeline gives the same answers on the Mac and on Windows, but differs from July for about 70% of sampled dams. The July environment and caches no longer exist, so July's numbers are treated as superseded by this rebuild.
- **New DEMs are written compressed.** `backend/build_trimmed_dems.py` writes every new DEM with lossless compression (ZSTD, or DEFLATE if unavailable), predictor 3, 512×512 tiles and BigTIFF safety, under both uv and conda.
- **Oversized flowlines are trimmed.** When a dam's flowline file covers more than 20 km on a side, the DEM is sized from the segments within 3 km of the dam's own reach. This prevents the ~72 × 78 km DEMs that broke four dams in `huc6_160503`.
- **DEM building under uv is fixed** (2026-11-06, see below). The first rebuild test under uv gave unusable DEMs; the new code reproduces `gdal.Warp`'s output.

## Rebuild test findings (2026-11-06)

A one-watershed rebuild (`huc6_030300`, 60 dams) under uv produced unusable results: 6 `ok`, 50 `negative_delta_wse`, 2 `no_dem`, 2 `insufficient_dem_coverage`. Dam 10217, for example, was `ok` (height 4.82 m) in the archive. Those results are in `lhd-staging\rebuild-test` and should not be used.

| Finding | Evidence | Cause |
| --- | --- | --- |
| **3 m DEMs were all zeros** | New `dem_10217.tif`: right size, location and compression, but every pixel 0.0; the archive's copy has 134–247 m. Affected all 49 dams using 1/9 arc-second (`ned19_…_nc_statewide_2003.img`) tiles. Reproduced: `rasterio.merge` on a test `.img` tile returns all zeros. | `rasterio.merge` mishandles those tiles' nodata value (−3.4 × 10³⁸); the log's repeated "Ignoring nodata value" warning. |
| **2 dams failed "CRS mismatch"** (3694, 9034) | Their tiles came from two UTM zones (`USGS_1M_17_…` and `USGS_1M_18_…`). | `rasterio.merge` rejects tiles in different projections. |
| 3 m data is **not** a regression | For the 16 dams in both tile lists, the archive used the same resolution (12 at 3 m, 4 at 1 m). | This part of North Carolina has no 1 m lidar in USGS's catalogue. |
| **Flowlines differ from the archive** | Dam 3601: archive flowline 1 segment (~0.4 km), new download 3 segments (~1.3 × 2.6 km), so a larger DEM. | The NLDI flowline service answers differently than in June. A full rebuild changes inputs, not just code. |
| New DEMs are compressed correctly | ZSTD, predictor 3, 512×512 blocks. | Working as intended. |

### Why it worked under conda and failed under uv

- **Under conda,** `build_trimmed_dems.py` stitched and clipped tiles with **`gdal.Warp`** from GDAL's Python bindings (`osgeo`), which conda installs. `gdal.Warp` reprojects tiles from different UTM zones onto one grid and honours each tile's nodata value.
- **Commit `839ad135` (2026-10-27, "Run with uv instead of conda: drop the GDAL Python bindings")** removed those bindings, because they can't be installed with `pip`/`uv sync` (no official wheels, especially on Windows), while rasterio's wheels bundle GDAL. The `gdal.Warp` calls were replaced with `rasterio.merge`, which was assumed to be equivalent but isn't: it rejects mixed projections and breaks on the 3 m tiles' nodata value. The commit was written with Claude's help and should have been tested on a fresh DEM build.
- **Why it went unnoticed:** every run after that commit reused existing DEMs ("skipping staging, all DEMs cached"), so the DEM-building code never ran until this rebuild test.

### The fix (2026-11-06, `backend/build_trimmed_dems.py`)

`_mosaic_and_clip` picks a method automatically and logs it once per run as `DEM mosaic method: …`:

| Environment | Method | Log line |
| --- | --- | --- |
| **uv** (no GDAL Python bindings) | `_mosaic_and_clip_rasterio`: `rasterio.warp.reproject`, rewritten to do what `gdal.Warp` does | `DEM mosaic method: rasterio reproject (no GDAL Python bindings; uv)` |
| conda (bindings installed) | `_mosaic_and_clip_gdal`: `gdal.Warp`, as before 2026-10-27 | `DEM mosaic method: gdal.Warp (GDAL Python bindings)` |

How the rewritten uv method copies `gdal.Warp`:

1. **Grid.** Uses the first tile's projection, and converts the dam's longitude/latitude box into it as a full envelope. It picks the nearest whole number of pixels and stretches the pixel size very slightly (e.g. 0.9997 m) so the box edges are exact.
2. **Reprojection.** Every tile is reprojected onto that grid with bilinear resampling, so tiles from a neighbouring UTM zone are converted instead of rejected.
3. **Nodata.** Each tile's own nodata value is passed explicitly, so the 3 m `.img` tiles keep their real elevations.
4. **Overlaps.** Where tiles overlap, the later tile's valid cells overwrite the earlier ones (`gdal.Warp`'s rule).

**Tests** (synthetic tiles with real-world layouts):

| Case | Result vs `gdal.Warp` on the same grid |
| --- | --- |
| 1 m tile in one UTM zone | identical (max difference 0.0 m) |
| Tiles from UTM zones 17 and 18 (like dams 3694, 9034) | max difference 0.00007 m; previously "CRS mismatch" |
| 1/9 arc-second `.img` tile (like dam 10217) | identical, 150–153 m; previously all zeros |
| Grid for dam 3601 from its archived flowline, vs the archived DEM | same 1111 × 1416 pixels and pixel size; origin about 0.27 m (a quarter pixel) apart, most likely a newer PROJ handling the WGS84→NAD83 step slightly differently |

All outputs were ZSTD, predictor 3, 512×512 tiles, with no warnings.

## Storage

| | Amount | Notes |
| --- | --- | --- |
| Kept on disk at the end | about 290 GB | ~16,300 compressed DEMs, flowlines, results |
| Temporary, during the run | up to about 50–100+ GB extra | raw USGS tiles for **one** watershed at a time; deleted after each watershed |
| **Free space to have** | **about 350–450 GB** | C: has about 700 GB free |
| Downloaded over the whole run | roughly 1–2.5 TB | spread over days; never stored all at once |

The run writes to `C:\Users\clmill00\fork-lhd-screening\lhd-staging` (internal NTFS drive). The external drive isn't used during the rebuild.

## Steps (uv)

Run every command in **PowerShell**, from the repository folder `C:\Users\clmill00\fork-lhd-screening\lhd-screening`. Nothing needs to be run from inside `lhd-staging`.

### 1. Get the latest code and environment

```powershell
cd C:\Users\clmill00\fork-lhd-screening\lhd-screening
git pull
uv sync
uv run python -c "import rasterio, geopandas, pynhd; print('ok, GDAL inside rasterio:', rasterio.__gdal_version__)"
```

- `git pull` fetches the latest commits (needed on any other computer; harmless here).
- `uv sync` installs the exact package versions from `uv.lock` into `.venv`.
- The last line checks the main libraries load. Expect `ok, GDAL inside rasterio: 3.x.x`.

### 2. Set Python to UTF-8 for this window

```powershell
$env:PYTHONUTF8 = "1"
```

The pipeline prints `→`; without UTF-8 mode, Python on Windows can crash on it when output is piped to `Tee-Object`. Set this in every new PowerShell window. (UTF-8 is the standard way to store text as bytes; older encodings like cp1252 have no `→`.)

### 3. Keep the PC awake

Settings → System → Power → Sleep: **Never** while plugged in. Change it back afterward.

### 4. Test on one watershed

```powershell
New-Item -ItemType Directory -Force C:\Users\clmill00\fork-lhd-screening\lhd-staging\rebuild-test-uv
Copy-Item C:\Users\clmill00\fork-lhd-screening\lhd-screening\data\full_lhd_website.csv C:\Users\clmill00\fork-lhd-screening\lhd-staging\rebuild-test-uv\dams.csv
uv run python backend\run_wsp_pipeline.py --local-staging-root C:\Users\clmill00\fork-lhd-screening\lhd-staging\rebuild-test-uv --dams-csv C:\Users\clmill00\fork-lhd-screening\lhd-staging\rebuild-test-uv\dams.csv --huc 030300 --min-free-gb 100 --workers 8 2>&1 | Tee-Object -FilePath C:\Users\clmill00\fork-lhd-screening\lhd-staging\rebuild-test-uv\run.log
```

| Part | What it does |
| --- | --- |
| `New-Item -ItemType Directory -Force …` | Makes the empty test folder (no error if it exists) |
| `Copy-Item … dams.csv` | A **copy** of the dam table that receives the results (the repo's copy stays untouched) |
| `uv run python backend\run_wsp_pipeline.py` | Runs the pipeline inside the uv environment |
| `--local-staging-root` | Folder for everything the run writes: flowlines, DEMs, results, the ledger |
| `--dams-csv` | The copy above |
| `--huc 030300` | Only this watershed (60 dams) |
| *(no `--existing-data-dir`)* | Rebuild from scratch: download fresh flowlines and tiles, build new DEMs |
| `--min-free-gb 100` | Don't start a watershed if C: has less than 100 GB free |
| `--workers 8` | Process 8 dams in parallel |
| `2>&1 \| Tee-Object -FilePath …` | Show output on screen and save it to `run.log` |

**Check for:** `DEM mosaic method: rasterio reproject (no GDAL Python bindings; uv)`, no `CRS mismatch`, no "Ignoring nodata value" warnings, and a mix of `ok` results instead of mostly `negative_delta_wse`. A new folder is used because the earlier `rebuild-test` results are invalid; that folder can be deleted.

**After the test:** Claude reads the folder to confirm the DEMs have real elevations and are compressed, that dams 3694 and 9034 now succeed, that dam 10217's DEM matches the archive's, and to estimate the full run's time and space.

### 5. Full rebuild

```powershell
New-Item -ItemType Directory -Force C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild
Copy-Item C:\Users\clmill00\fork-lhd-screening\lhd-screening\data\full_lhd_website.csv C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild\dams.csv
uv run python backend\run_wsp_pipeline.py --local-staging-root C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild --dams-csv C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild\dams.csv --min-free-gb 100 --workers 8 2>&1 | Tee-Object -FilePath C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild\run.log
```

Same as the test without `--huc`: all 306 HUC-6 watersheds, about 15,700 dams (18,804 in the table minus 3,102 reviewed as not LHDs and 10 with no HUC code).

**Check progress** (second PowerShell window):

```powershell
cd C:\Users\clmill00\fork-lhd-screening\lhd-screening
uv run python backend\run_wsp_pipeline.py --local-staging-root C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild --dams-csv C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild\dams.csv --status
```

**Resume after any stop** (Ctrl+C, restart, error): open PowerShell, then

```powershell
cd C:\Users\clmill00\fork-lhd-screening\lhd-screening
$env:PYTHONUTF8 = "1"
uv run python backend\run_wsp_pipeline.py --local-staging-root C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild --dams-csv C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild\dams.csv --min-free-gb 100 --workers 8 2>&1 | Tee-Object -FilePath C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild\run.log -Append
```

The ledger (`wsp_ledger.json`) and each dam's result file mark finished work, so it picks up where it stopped. `-Append` keeps the earlier log.

### 6. After the run

```powershell
robocopy C:\Users\clmill00\fork-lhd-screening\lhd-staging\full-rebuild D:\lhd-runs\full-rebuild /E
```

`robocopy … /E` copies the whole folder, subfolders included, to the external drive as a backup. Then check the results (statuses, failures, comparison with the archive and NID heights), merge them into the repository's `data\full_lhd_website.csv`, and commit.

## Alternative: conda

The pipeline also runs under conda, where it builds DEMs with `gdal.Warp` itself. Use this only if the uv test shows a DEM problem. Install Miniforge (<https://github.com/conda-forge/miniforge/releases>, "Just Me"), then:

```powershell
& "$env:USERPROFILE\miniforge3\Scripts\conda.exe" init powershell
# close and reopen PowerShell; if scripts are blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
cd C:\Users\clmill00\fork-lhd-screening\lhd-screening
conda env create -f environment.yml
conda install -n lhd-environment -c conda-forge pyarrow scipy scikit-learn matplotlib
conda activate lhd-environment
```

Then use the same commands as above with `python` in place of `uv run python`. The log should say `DEM mosaic method: gdal.Warp (GDAL Python bindings)`. (`environment.yml` lacks pyarrow, scipy, scikit-learn and matplotlib, which `pyproject.toml` has; the second `conda install` line adds them.)

## What to watch for

| You see | Meaning | Action |
| --- | --- | --- |
| `DEM mosaic method: rasterio reproject …` or `… gdal.Warp …` | Which DEM method is in use | Nothing |
| `CRS mismatch` or `Ignoring nodata value` | Old code is running | Stop (Ctrl+C); `git log -1` should show the uv DEM fix commit |
| `UnicodeEncodeError … '\u2192'` | UTF-8 mode not set | `$env:PYTHONUTF8 = "1"` and rerun |
| `ΓåÆ` in the output | `→` shown with the wrong display character set | Harmless |
| `flowline spans … km; using … segment(s) within 3 km of the dam reach` | Oversized-flowline trim working (expected around `huc6_160503`) | Nothing |
| `WARN … the dam reach was not found in it` | An oversized flowline that couldn't be trimmed | Note the dam |
| `FATAL: HUC6 … failed` / `Halting` | A watershed hit an error | Copy the last lines for Claude; rerun resumes after fixing |
| `FutureWarning … incompatible dtype` | pandas warning, `run_wsp_pipeline.py` line 921 | Harmless for now |

## Known issues (not blocking)

- **Dam-table encoding.** The pipeline reads the dam table as latin-1 and writes it as UTF-8, which garbles its 41 special characters (`’`, `ñ`) further on every write. Using a copy of the table keeps the repository's file clean; a code fix is planned.
- **Fragile downstream point picker.** `pick_downstream_node` (`backend/screening/wsp.py`, since 2026-07-01) picks the single cell whose local slope best matches the reach slope within 10–500 m downstream; small input differences can move it hundreds of metres. It is deterministic, so the rebuild is consistent, but it may need smoothing later.
- **Stream width.** Crest length comes from the hydrofabric's bankfull width (`owp_tw_bf`), not the DEM; it changes heights by centimetres at median flow, but matters more for the danger range.
- **Memory.** The uv DEM method holds each dam's DEM in memory while building it (as `rasterio.merge` did). With oversized flowlines now trimmed this is typically tens of MB and at most about 0.5 GB per worker.

## Glossary

- **HUC-6:** a 6-digit USGS watershed code; the pipeline processes one HUC-6 at a time.
- **DEM:** digital elevation model; here, a small elevation raster clipped around each dam.
- **Flowline:** the NHD river line for a dam's reach and its neighbours (`STRM\<dam_id>\nhd_flowline_<COMID>.gpkg`).
- **COMID:** the NHDPlus ID of a river reach.
- **UTM zone / CRS:** the map projection a tile uses; USGS 1 m tiles near a zone edge can come in two zones.
- **nodata value:** the special number a raster uses to mark "no elevation here".
- **Reproject / resample:** convert a raster onto another grid or projection; bilinear resampling averages the four nearest cells.
- **WSP:** water-surface profile; the step that samples the DEM along the river to estimate dam height.
- **uv / conda:** Python environment managers. uv installs from `pyproject.toml`/`uv.lock` (no GDAL Python bindings); conda (via Miniforge) installs from `environment.yml` and includes them.
- **Ledger:** `wsp_ledger.json` in the run folder; records which watersheds are finished so runs can resume.
