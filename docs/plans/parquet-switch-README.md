# Parquet switch: the dam table moves from CSV to Parquet

Branch: `fix/uv-dem-mosaic`. Written November 2026 by Claude for Caleb Miler.

This README explains what was changed, why, how it was tested, and the exact
commands to run. Nothing below has been run on the Windows PC yet. You run each
step after reviewing it.

Related files:

- `docs/plans/deferred-fixes-after-parquet.md`: everything recommended but left out of this change (service retries, smaller pipeline fixes, other encoding spots).
- `docs/plans/uv-run-2-plan.md`: the UV RUN 2 plan these fixes come from.

---

## 1. Why

The master dam table was `data/full_lhd_website.csv`. A CSV is plain text, so
every program reading it must be told the text encoding. Ten scripts read it as
**latin-1** and wrote it back as **UTF-8**. The table really is UTF-8, so on
every save the 41 special characters (`ñ`, `’`, ...) doubled in size. In the
November 2026 full rebuild that grew `full-rebuild\dams.csv` to **48.9 GB**, and
the heights and danger ranges were never written.

**Parquet** stores text as UTF-8 by definition. There is no encoding setting to
get wrong, so this failure cannot happen again. Other benefits:

- It stores each column's type (number or text).
- It is about 5 times smaller: 9.0 MB as CSV, 1.83 MB as Parquet.

Now **every script and the website** read and write `data/full_lhd_website.parquet`,
through one shared module.

## 2. What changed

### 2.1 New files

| File | What it does |
| --- | --- |
| `backend/lhd_processor/dam_table.py` | The **only** code that reads or writes the dam table. `read_dam_table(path)` and `write_dam_table(df, path)`. All the rules in section 3 live here. |
| `scripts/convert_dam_table_to_parquet.py` | One-time converter: reads `data/full_lhd_website.csv` strictly as UTF-8, writes `data/full_lhd_website.parquet`, reads it back and compares everything. Prints `PASS` or `FAIL`. It never changes or deletes the CSV, and refuses to overwrite an existing Parquet without `--force`. |
| `docs/plans/deferred-fixes-after-parquet.md` | Fixes for later (see the top of this file). |
| `docs/plans/parquet-switch-README.md` | This file. |
| `data/full_lhd_website.parquet` | **Not delivered.** You create it with the converter (step 2 of section 6) and commit it. |

### 2.2 Pipeline (the scripts the full rebuild runs)

| File | Change |
| --- | --- |
| `backend/run_wsp_pipeline.py` | Reads and writes the table through `dam_table.py`. Default is `data/full_lhd_website.parquet`. New flag `--dams-table`; the old `--dams-csv` still works as another name for it. Each watershed folder now gets `dams.parquet` and `dams_needs_staging.parquet` instead of `dams.csv` and `dams_needs_staging.csv`. |
| `backend/stage_nhd_dem.py` | Reads the watershed's `.parquet` table. Flag `--dams-table` (old `--dams-csv` still accepted). `tile_manifest.csv` is not the dam table and stays CSV. |
| `backend/compute_wsp_danger_range.py` | Reads and writes the Parquet table. Flag `--table` (old `--csv` still accepted). It no longer writes a second copy to `frontend/data/full_lhd_website.csv`; the website reads `data/` directly (section 4). |

### 2.3 Other backend scripts

All now use `dam_table.py`, default to the Parquet table, and keep their old
flag name as an alias. Their own output files (reports, diagnostics, figures)
are unchanged.

| File | Reads / writes the table | Old flag still accepted |
| --- | --- | --- |
| `backend/analyze_wsp_predictions.py` | reads | `--csv` (new `--table`) |
| `backend/assign_huc8.py` | reads and **writes** (HUC columns) | `--dams-csv` (new `--dams-table`) |
| `backend/assign_nhd_comid.py` | reads and **writes** (`Reach_ID`, `GNIS_Name`) | `--csv` (new `--table`) |
| `backend/audit_non_lhd_outputs.py` | reads (its default used to be the frontend copy) | `--csv` (new `--table`) |
| `backend/build_nwm_fdc.py` | reads (`Reach_ID` only) | `--csv` (new `--table`) |
| `backend/build_synthetic_rating_curves.py` | reads | `--csv` (new `--table`) |
| `backend/diagnose_failures.py` | reads (join columns only) | `--dams-csv` (new `--dams-table`) |
| `backend/diagnose_wsp_failures.py` | reads (join columns only) | `--dams-csv` (new `--dams-table`) |
| `backend/estimate_dam_height_lite.py` | reads | `--csv` (new `--table`) |
| `backend/fetch_lhd_review_metadata.py` | reads and **writes** (review columns) | new optional `--table` |
| `backend/validate_dam_height_lite.py` | reads | `--dam-csv` (new `--dam-table`) |

`fetch_lhd_review_metadata.py` had a second encoding problem: it opened the CSV
with no encoding at all, so on Windows Python used cp1252. Its backup is now
`full_lhd_website.bak.<date_time>.parquet` in the same folder.

### 2.4 Utility scripts (`scripts/`)

| File | Reads / writes the table | Old flag still accepted |
| --- | --- | --- |
| `scripts/enrich_gnis_name.py` | reads and **writes** `GNIS_Name` | `--csv` (new `--table`); `--out` must end in `.parquet` |
| `scripts/enrich_nid_height.py` | reads and **writes** NID heights and lengths | `--csv` (new `--table`); `--out` must end in `.parquet` |
| `scripts/merge_flat_into_huc.py` | reads | `--dams-csv` (new `--dams-table`) |
| `scripts/osm_rename_dams.py` | reads and **writes** `OSM_Name` | (no flags) |

These live in `scripts/`, so each adds `backend/` to Python's search path before
importing `dam_table.py`.

### 2.5 Website and docs

| File | Change |
| --- | --- |
| `frontend/mapping-logic.js` | `loadDams()` reads `data/full_lhd_website.parquet` instead of `d3.csv(...csv)` (section 4). |
| `frontend/.gitignore` | Ignores the local-dev copy `frontend/data/full_lhd_website.parquet` (and still the old CSV copy). |
| `README.md` | Mentions the Parquet table, the `--dams-table` flag, a Parquet version of the "dams per HUC6" command, and how to copy the table for local website testing. |

### 2.6 Deliberately not changed

- `backend/arc_archive/*`: the retired ARC pipeline (see the deferred-fixes file).
- Scripts that read other CSVs, not the dam table (for example `scripts/compare_height_methods.py` and `scripts/paper_stats_summary.py`).
- `data/full_lhd_website.csv` itself stays in the repository until you remove it (section 6, step 7).

## 3. How `dam_table.py` works

### Reading

`read_dam_table(path, columns=None, as_str=False)`

- `path` must end in `.parquet`. A `.csv` path stops with a message that includes the converter command, so an old command can't silently use the CSV.
- `columns=[...]` reads only those columns (used by the diagnostics scripts).
- `as_str=True` returns every value as text, missing values stay missing. This is for scripts written around `pd.read_csv(..., dtype=str)`.

### Writing

`write_dam_table(df, path)`

**Column types stay exactly as in the CSV era.** Parquet stores types, but
several scripts work with every cell as text. Saving that as-is would turn
number columns into text permanently and break arithmetic such as
`Dam_Height_WSP_Ft > 0`.

So before saving, the text columns (and pandas' nullable `Int64`/`Float64`/
`boolean` columns) are re-typed in memory using the same rules `pd.read_csv`
applied when it read the old CSV:

| In memory before saving | Stored as | Same as the CSV era |
| --- | --- | --- |
| `""` (empty text) | missing | yes |
| text that is all numbers, e.g. `"35.2"` | number | yes |
| a column mixing text and numbers | text | yes |
| `Int64` with gaps (e.g. `Reach_ID`) | decimal number (`13080001.0`) | yes |
| HUC code text `"01020304"` | number `1020304` | yes, and every user re-pads it with `zfill(8)` |
| plain number columns | unchanged | yes |

**Safe saving.** The table is written to a temporary file in the same folder
(`<name>.<process id>.tmp`) and then swapped in with `os.replace`. A crash
mid-save leaves the old table intact. If Windows reports the file as locked (open
in another program, antivirus scan), it retries 5 times, 2 seconds apart. After
that it stops and says to close the other program.

**Fixed engine.** It always uses `pyarrow` with Snappy compression. Your
environment also has `fastparquet`, and pandas' automatic choice could differ
between machines. Snappy is what the website's reader decodes natively.

## 4. Website

The site is a plain `<script>` (not a module), so `mapping-logic.js` loads the
Parquet reader with a dynamic `import()`:

- **Reader:** hyparquet, a small Parquet reader with no dependencies. Pinned to version **1.32.0** (released 2026-10-29), loaded from `https://cdn.jsdelivr.net/npm/hyparquet@1.32.0/+esm`.
- **Loading:** the whole file (about 1.8 MB) is fetched once, with the same `?v=<time>` cache-buster as before.
- **Same rows as before:** each row becomes the plain object of text values that `d3.csv` used to return, so the rest of the 2,680-line file is unchanged:
  - missing → `''`;
  - whole numbers (which arrive as JavaScript `BigInt`) → their digits;
  - decimals → JavaScript's shortest form (`78840` instead of `78840.0`);
  - true/false → `'True'`/`'False'`.
- **Number formatting is safe:** the only difference from the CSV is that formatting. Every affected column is read with `parseFloat`/`parseInt`, and every `Reach_ID` use already strips a trailing `.0`, so nothing on the page changes.
- **Deployment:** the GitHub Pages workflow (`.github/workflows/pages.yml`) already copies the whole `data/` folder into the site, so `data/full_lhd_website.parquet` is published automatically. It deploys only from `main`, so the live site changes when this branch is merged.

## 5. How it was tested (in Claude's sandbox)

The sandbox uses the same versions as the PC: Python 3.10, pandas 2.3.3,
pyarrow 25.0.1, numpy 2.2.6. Network-dependent services (USGS NLDI, TNM,
hydroshare, Lynker S3, jsDelivr) are blocked from the sandbox, so those parts
are replaced by small fakes or must be checked on the PC (section 6).

### 5.1 Converter

- Both copies of the CSV convert to the same file (SHA-256 `f57d97ce…5ef46`): the repo copy (`\n` line breaks) and your Windows copy (`\r\n` line breaks inside 7 review notes).
- `PASS`: 18,804 rows × 93 columns, identical values and types, all 41 special characters intact.

### 5.2 `dam_table.py`

- 30 save/load cycles: no change at all.
- Text-mode (`as_str=True`) round trip: types restored.
- Mixed text/number columns: saved (pyarrow alone refuses them).
- `.csv` paths: refused with the converter command.
- Nullable `Int64`, nullable `string` and `boolean`: stored as the CSV produced them.

### 5.3 Pipeline

| Test | What | Result |
| --- | --- | --- |
| A | Old CSV code vs new Parquet code, aggregating the 60 results of watershed 030300 and computing danger ranges | Heights, lengths, Q, ΔWSE and upstream COMIDs identical. Qmin/Qmax differ by about 1e-16 relative (CSV text rounding; Parquet stores exact values). Special characters: Parquet 41; the old CSV code 182 after two saves. |
| B | The full `run_wsp_pipeline.py` command with nothing left to compute (rerun after the last change to `dam_table.py`) | Exit 0; 49 dams updated, 49 danger ranges solved; column types unchanged; no leftover `.tmp` files. |
| C | Staging path: `_process_huc` writes `dams.parquet` + `dams_needs_staging.parquet`, `stage_nhd_dem.py` reads it for all 67 dams, manifest written, `build_trimmed_dems.py` called with `--dams-table` | All as expected; the per-watershed table equals the master subset value for value. |

### 5.4 The other 15 scripts

Each ran its real `main()` on a copy of the table. Network calls were replaced
by fakes. Every script that saves the table was checked for: other columns
unchanged in value **and** type, special characters intact.

| Script | Test | Result |
| --- | --- | --- |
| `analyze_wsp_predictions.py` | old `--csv` flag, figure from the test-B table | PASS |
| `diagnose_wsp_failures.py` | 030300 results | PASS: 67 rows, names joined for all 67 |
| `diagnose_failures.py` | 030300 folder | PASS: 60 rows, names joined |
| `audit_non_lhd_outputs.py` | non-LHD list + full run | PASS: 3,102 dams, same as a direct count |
| `merge_flat_into_huc.py` | old `--dams-csv` flag, dry run | PASS: dam 3597 → 030300 |
| `validate_dam_height_lite.py` | join on a copy with the ARC column added | PASS (the real table lacks that column; pre-existing, see the deferred file) |
| `build_nwm_fdc.py`, `build_synthetic_rating_curves.py`, `estimate_dam_height_lite.py` | start-up, defaults, `Reach_ID` read | PASS: 11,819 reaches |
| `assign_huc8.py` | fake WBD layer with each dam's current HUC8 | PASS: whole table unchanged |
| `assign_nhd_comid.py` | checkpoint of current `Reach_ID`s, fake GNIS lookup | PASS: `Reach_ID` stays float64 |
| `fetch_lhd_review_metadata.py` | fake NID answers with `ñ`, `’`, line breaks for 3 dams | PASS: 3 rows updated, 2 new columns, backup written, other dams unchanged |
| `enrich_gnis_name.py` | fake lookup returning current names | PASS: table unchanged |
| `enrich_nid_height.py` | fake 3-dam NID file | PASS: only blanks filled, existing values kept |
| `osm_rename_dams.py` | fake Overpass answer "Presa Nuñez" | PASS: one dam renamed, others unchanged |

### 5.5 Website

The exact loader code from `mapping-logic.js`, with hyparquet 1.32.0 from npm
(the same code jsDelivr serves), was compared cell by cell with `d3.csv`'s parse
of the CSV:

- 18,804 rows, same columns in the same order.
- 1,578,597 cells identical.
- 170,175 cells differ only in number formatting (`78840.0` vs `78840`); same value under `parseFloat`/`parseInt`.
- **0 real differences**; 41 special characters intact.

**Not tested in the sandbox** (do it on the PC, section 6):

- a real pipeline run with live USGS services;
- the website in a real browser loading hyparquet from jsDelivr.

### 5.6 Delivery

- Before writing, each of the 21 changed files on the PC was checked: identical to the branch and unmodified since the clone.
- All 24 files were written with Windows line endings, read back from the PC, and compared byte for byte: 24 of 24 identical.

## 6. What you run

All commands are PowerShell, from the repository folder:

```powershell
cd C:\Users\clmill00\fork-lhd-screening\lhd-screening
```

### Step 1: see what changed

```powershell
git status
git diff --stat
```

- `git status` should list 21 modified files and 4 new files: the 3 in section 2.1 and this README.
- `git diff --stat` shows how many lines changed in each.
- Review the diffs (e.g. in VS Code's Source Control panel) before going on.

### Step 2: make sure the environment matches the lock file

```powershell
uv sync --frozen
```

- `uv sync` installs exactly what `uv.lock` lists into `.venv`.
- `--frozen` means "use `uv.lock` as it is; never update it". No new packages are needed for this change.

### Step 3: convert the table

```powershell
uv run --frozen python scripts\convert_dam_table_to_parquet.py
```

- `uv run --frozen` runs Python from the project's `.venv` without touching `uv.lock`.
- `scripts\convert_dam_table_to_parquet.py` reads `data\full_lhd_website.csv` and writes `data\full_lhd_website.parquet`.

Expected output (paths shortened here):

```
Reading ...\data\full_lhd_website.csv
  18,804 rows x 93 columns, 41 special characters
  normalised Windows line breaks inside 7 text cell(s)
Wrote ...\data\full_lhd_website.parquet (1.83 MB; CSV was 8.97 MB)
PASS: 18,804 rows, 93 columns, identical values and types, 41 special characters intact.
```

Optional fingerprint check:

```powershell
Get-FileHash data\full_lhd_website.parquet -Algorithm SHA256
```

- `Get-FileHash` prints the file's SHA-256 fingerprint.
- Expected: `F57D97CE23191CC7D9C06394F761771EFBE4CA87E08563091FCFDDC123D5EF46`, the same file the sandbox made.
- If it differs but step 3 printed `PASS`, the file is still correct (a different library build can change the bytes, not the data). Tell Claude and it can be checked.

### Step 4: quick checks (seconds)

```powershell
uv run --frozen python backend\run_wsp_pipeline.py --help
uv run --frozen python -c "import pandas as pd; d=pd.read_parquet('data/full_lhd_website.parquet'); print(d.shape); print(d[['OBJECTID','Dam_Name','HUC8','Reach_ID']].head())"
```

- The first prints the pipeline's options. Look for `--dams-table DAMS_TABLE, --dams-csv DAMS_TABLE`.
- The second opens the new table with pandas, prints its size (should be `(18804, 93)`) and the first 5 rows of four columns.

### Step 5: smoke test, one watershed into a new folder (minutes; needs internet)

This runs the real pipeline, with live USGS downloads, on the small watershed
030300 (67 dams). It uses a **copy** of the table in a new folder, so neither
`data\full_lhd_website.parquet` nor `lhd-staging\full-rebuild` is touched.

```powershell
New-Item -ItemType Directory C:\Users\clmill00\fork-lhd-screening\lhd-staging\parquet-smoke
Copy-Item data\full_lhd_website.parquet C:\Users\clmill00\fork-lhd-screening\lhd-staging\parquet-smoke\dams.parquet
uv run --frozen python backend\run_wsp_pipeline.py --local-staging-root C:\Users\clmill00\fork-lhd-screening\lhd-staging\parquet-smoke --dams-table C:\Users\clmill00\fork-lhd-screening\lhd-staging\parquet-smoke\dams.parquet --huc 030300
```

- `New-Item -ItemType Directory ...` creates the empty test folder `lhd-staging\parquet-smoke`.
- `Copy-Item ... dams.parquet` puts a copy of the table in it; the run writes heights into this copy.
- `--local-staging-root ...\parquet-smoke` is where the run keeps its DEMs, flowlines, results and `wsp_ledger.json`.
- `--dams-table ...\parquet-smoke\dams.parquet` is the table it reads and updates.
- `--huc 030300` processes only watershed 030300.
- The pipeline stops cleanly if the drive has less than 200 GB free (`--min-free-gb`, default 200). If that happens, add `--min-free-gb 20` to the command; this test needs well under 1 GB.

Then check the result:

```powershell
uv run --frozen python -c "import pandas as pd; d=pd.read_parquet(r'C:\Users\clmill00\fork-lhd-screening\lhd-staging\parquet-smoke\dams.parquet'); s=d[d.HUC6==30300]; print(len(s), 'dams;', s.Dam_Height_WSP_Ft.notna().sum(), 'with height;', s.Qmin_env.notna().sum(), 'with danger range')"
dir C:\Users\clmill00\fork-lhd-screening\lhd-staging\parquet-smoke\huc6_030300
```

- The first line reads the test table and counts the dams of watershed 030300 with a height and with a danger range. `HUC6` is stored as a number, so it's `30300`. Earlier runs got 49 of 67; a similar number is expected, and fewer can mean USGS outages (the problems in the deferred-fixes file).
- `dir ...\huc6_030300` should show `dams.parquet`, `DEM`, `STRM`, `WSP_RESULTS` and `tile_manifest.json`, and no `dams.csv`.
- The test table should stay about 1.8 MB.
- Anything printing `Traceback` or `WARN aggregate failed`: stop and send Claude the output.

### Step 6: website check on your PC

```powershell
Copy-Item data\full_lhd_website.parquet frontend\data\full_lhd_website.parquet
cd frontend
uv run --frozen python -m http.server 8000
```

- `Copy-Item ...` puts the table where the local site looks for it. The copy is gitignored, so it won't be committed.
- `cd frontend` moves into the website folder.
- `python -m http.server 8000` serves that folder at `http://localhost:8000`. It keeps running until you press Ctrl+C.

Open `http://localhost:8000` in a browser:

- The map should show the dam markers as before.
- Open a dam's popup and its charts.
- Press F12 → Console: there should be no `Error loading the dam table (Parquet)` message.

Press Ctrl+C in PowerShell to stop the server, then `cd ..` to return to the repository folder.

### Step 7: commit (after steps 3–6 look right)

```powershell
git add backend\lhd_processor\dam_table.py scripts\convert_dam_table_to_parquet.py data\full_lhd_website.parquet
git add backend\run_wsp_pipeline.py backend\stage_nhd_dem.py backend\compute_wsp_danger_range.py
git add backend\analyze_wsp_predictions.py backend\assign_huc8.py backend\assign_nhd_comid.py backend\audit_non_lhd_outputs.py backend\build_nwm_fdc.py backend\build_synthetic_rating_curves.py backend\diagnose_failures.py backend\diagnose_wsp_failures.py backend\estimate_dam_height_lite.py backend\fetch_lhd_review_metadata.py backend\validate_dam_height_lite.py
git add scripts\enrich_gnis_name.py scripts\enrich_nid_height.py scripts\merge_flat_into_huc.py scripts\osm_rename_dams.py
git add frontend\mapping-logic.js frontend\.gitignore README.md docs\plans\deferred-fixes-after-parquet.md docs\plans\parquet-switch-README.md
git status
git commit -m "Store the dam table as Parquet (data/full_lhd_website.parquet)" -m "All scripts and the website read and write the table through backend/lhd_processor/dam_table.py. Fixes the latin-1/UTF-8 mismatch that grew full-rebuild/dams.csv to 48.9 GB. Old --csv/--dams-csv flags still accepted." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin fix/uv-dem-mosaic
```

- The five `git add` lines stage exactly the 26 files of this change, by name, so nothing else in `backend` or `scripts` is included by accident. `data\full_lhd_website.parquet` is the table you made in step 3.
- `git status` (again) lets you check that nothing else is staged, e.g. not `docs\plans\uv-run-2-plan.md` if you're keeping it for a separate commit.
- `git commit -m ... -m ...` makes the commit: the first `-m` is the title, the next ones are paragraphs of the message.
- `git push origin fix/uv-dem-mosaic` uploads the branch to GitHub. The live website changes only when the branch is merged into `main`.

**Removing the old CSV (optional, later):** once you're satisfied, run
`git rm data\full_lhd_website.csv` and commit it. `git rm` removes the file
and records the removal (it stays in the git history). Keep
`data\full_lhd_website.archivedkenney.csv`; it is the comparison baseline.

## 7. Things to know from now on

- **Excel can't open `.parquet`.** To look at the table, use pandas (step 4), DuckDB, or a viewer such as Tad. That's intentional: re-saving the table from Excel is a classic way to mix encodings.
- **A `.csv` path is refused** by every script with a message that shows the converter command. Old flag names still work but need a `.parquet` file.
- **The UV RUN 2 rerun** will use `full-rebuild\dams.parquet`, copied from `data\full_lhd_website.parquet`. The damaged 48.9 GB `full-rebuild\dams.csv` is no longer used (see the deferred-fixes file, section 3).
- **`frontend\data\full_lhd_website.csv` is no longer updated** by the pipeline; the website reads `data\full_lhd_website.parquet`.
- **Still to do:** see `docs/plans/deferred-fixes-after-parquet.md`. The service-retry fixes there must be in place before the affected-dams rerun, or it will hit the same NLDI/TNM failures.
