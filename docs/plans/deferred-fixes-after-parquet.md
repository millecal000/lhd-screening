# Deferred fixes after the Parquet switch

Branch: `fix/uv-dem-mosaic`. Written November 2026.

The branch now stores the master dam table as **`data/full_lhd_website.parquet`**.
Every pipeline script, utility script and the website read and write that file
through one module, `backend/lhd_processor/dam_table.py`. That removes the
latin-1/UTF-8 mismatch that grew `full-rebuild\dams.csv` to 48.9 GB.

Everything below was recommended during the November 2026 full-rebuild analysis
and the UV RUN 2 plan (`docs/plans/uv-run-2-plan.md`), but it is **not** part of
the Parquet change. Line numbers are for the branch after the Parquet change.

---

## 1. Service failures during the full rebuild (UV RUN 2 fixes 2–4)

These caused most of the wrong or missing results in the full rebuild. Each one
is designed in detail in `docs/plans/uv-run-2-plan.md`.

| # | Problem in the full rebuild | Dams | Fix (plan section) |
| --- | --- | --- | --- |
| 1.1 | NLDI returned "no features" during outages, so only the dam's own reach was saved as its flowline ("seed-only") | 6,238 | Direct `requests` navigation with retries; an empty direction is a failure, not an answer; health check and circuit breaker; no `.gpkg` or DEM for a failed dam (section 4) |
| 1.2 | TNM HTTP 500s: dam cached as permanent `no_dem` | 160 | 3 retries per dataset, `TnmUnavailable`, `tnm_unavailable.json` marker instead of a permanent failure (section 5) |
| 1.3 | A failed 1 m request treated as "no 1 m data", so a coarser DEM was used | 88 | Fall back to a coarser dataset only after a successful empty answer (section 5) |
| 1.4 | No DEM tiles in 3 watersheds (031401, 031402, 110701) during outages | 48 (part of 208 "No DEM tiles found") | Included in the reset (section 8 c) |
| 1.5 | Flowline missing one side ("half flowline") | about 102 candidates | Over-inclusive check inside the 73 outage watersheds (section 8 d) |
| 1.6 | `build_trimmed_dems.py` writes every non-`ok` dam to `dam_failures.json`; listed dams are skipped forever | all of the above | Never write `api_fail` / `tnm_unavailable` dams there; the reset removes them (section 5) |
| 1.7 | No flowline because the dam has no `Reach_ID` | 121 (of 835 without `Reach_ID`) | Reporting only; COMIDs assigned in a separate task with `backend/assign_nhd_comid.py` (section 6) |

## 2. Smaller pipeline corrections (UV RUN 2 section 7)

- **`backend/run_wsp_pipeline.py` 623–624:** when a watershed has no pending dams, `stage_table = huc_table` restages every dam and re-downloads every tile. Change it to `None`.
- **`backend/run_wsp_pipeline.py` 693–705 (`_update_csv`):** a failed aggregate or danger-range step only prints `WARN` and the run still "finishes". This is how the 48.9 GB failure went unnoticed. Make it a loud error (`PIPELINE ERROR:`) with a non-zero exit code. The function name still says CSV; renaming it to `_update_table` is cosmetic.
- **`backend/stage_nhd_dem.py` `save_manifest` (line 315):** rewrites `tile_manifest.json` with only the dams just staged. Make it merge with the existing entries.
- **VAA table load (`backend/run_wsp_pipeline.py` around 788):** a failed `load_vaa_df()` is only a warning, and every dam is then cached as an `exception:` result. Retry 3 times, then stop with `PIPELINE ERROR: VAA table failed to load`.
- **Unique error marker:** print `PIPELINE ERROR:` for every real failure, so log checks aren't confused by `NLDI Navigation Error` or PowerShell `NativeCommandError` lines.
- **`--finalize` flag:** label dams still failing after 3 healthy-service attempts, promote watersheds to `ready_to_archive`, and run the final aggregate.
- **`_cmd_aggregate` searches the whole run folder** (`rglob`) for `wsp_result.json`. Never put backups inside the run folder.

## 3. Resetting only the affected dams (UV RUN 2 section 8)

A new `backend/reset_affected_dams.py` (dry run by default) moves the results of
the 5,806 affected dams to a backup folder outside the run folder. It also
removes their entries from `tile_manifest.json` and `dam_failures.json` and
marks their watersheds `partial`, so a rerun only redoes those dams. It depends
on sections 1 and 2 being in place first; otherwise the rerun would reproduce
the same failures.

**With Parquet, the run copy of the dam table is rebuilt instead of repaired.**
The rerun uses `full-rebuild\dams.parquet`, copied from
`data\full_lhd_website.parquet`. The aggregate step refills heights and danger
ranges from every `wsp_result.json` in the run folder. The damaged 48.9 GB
`full-rebuild\dams.csv` is no longer needed. Note: the aggregate only overwrites
dams with an `ok` result; every other dam keeps the values it has in
`data\full_lhd_website.parquet`. That is the existing behaviour, and it should be
decided on before the final table is published.

## 4. Other encoding spots (not the dam table)

Found while porting. None of them can grow or garble the dam table, which is why
they were left out of the Parquet change.

| File | Line | What | Risk | Fix |
| --- | --- | --- | --- | --- |
| `scripts/enrich_nid_height.py` | 71 | Reads the NID national CSV (`frontend/data/nid_nation.csv`) as latin-1. The file is valid UTF-8 (329 non-ASCII bytes). | Low: only numeric NID fields and the Federal ID are copied into the dam table | `encoding="utf-8-sig"` |
| `scripts/enrich_nid_height.py` | 67 | Reads the NID preamble line with Windows' default encoding | Low: only the date is used | `encoding="utf-8-sig"` |
| `scripts/osm_rename_dams.py` | log writes | `osm_rename_log.csv` and `osm_rename_progress.log` are written with Windows' default encoding (cp1252) | Medium: an OSM name with a character cp1252 lacks would crash the script after the table is written | `encoding="utf-8"` on both `open()` calls |
| `backend/run_wsp_pipeline.py` and others | many | JSON files opened without `encoding=` | Very low: `json.dump` writes ASCII by default | Add `encoding="utf-8"` for consistency |
| PowerShell `Tee-Object` logs | – | PowerShell 5.1 writes UTF-16 logs; `→` and `—` show as `ΓåÆ` and `ΓÇö` | Cosmetic; log readers must expect UTF-16 | Use `Out-File -Encoding utf8` or PowerShell 7 |

## 5. Pre-existing problems found while testing the port

- **`backend/validate_dam_height_lite.py` (line 56) and `backend/estimate_dam_height_lite.py` (line 1044, only when `--objectids` is not given)** need the column `Dam_Height_GIS_Ft`, which came from the retired ARC pipeline and is not in the current dam table. They stop with `KeyError: 'Dam_Height_GIS_Ft'`, as they would have with the CSV. Decide whether to retire these scripts or compare against `Dam_Height_WSP_Ft` instead.
- **`backend/arc_archive/*`** (retired ARC pipeline) still defaults to `data/full_lhd_website.csv` and was not ported. These scripts now fail with a clear "not found" message unless given a CSV. Port them or delete them.
- **Old docs** `docs/plans/full-rebuild-windows-runbook.md` and `docs/plans/lhd-dem-storage-plan.md` still mention the CSV. They describe past runs and were left as they are.

## 6. Parquet follow-ups

- **Remove the CSV from the repository** after the Parquet file is checked: `git rm data\full_lhd_website.csv` plus the `data\*.csv.*.bak` backups. `data\full_lhd_website.archivedkenney.csv` stays as the comparison baseline.
- **HUC codes are stored as numbers** (for example `01020304` becomes `1020304`), exactly as the CSV-era pipeline read them. Every user re-pads them with `zfill(8)`. Storing them as text would be cleaner but means changing those users.
- **`Reach_ID` is stored as a decimal number** (`13080001.0`) because some dams have none, as before. A whole-number type with gaps (`Int64`) would be cleaner. The website already strips `.0`.
- **Website reader:** `frontend/mapping-logic.js` loads hyparquet 1.32.0 from jsDelivr. 1.33.0 (2026-11-08) exists; upgrade after testing. To remove the CDN dependency, copy hyparquet's files into `frontend/` and import them locally.
- **Parquet-specific tools:** to look at the table, use `uv run --frozen python -c "import pandas as pd; print(pd.read_parquet('data/full_lhd_website.parquet').head())"`, or a viewer such as DuckDB or Tad. Excel cannot open Parquet. That is deliberate: a table saved from Excel is how encodings get mixed.

## 7. DEM storage (discussed, not decided)

DEMs, not the dam table, are what take the space. Measured on sample DEMs (69.8 MB as stored now, ZSTD):

| Option | Size | Note |
| --- | --- | --- |
| ZSTD level 22 | 68.4 MB | About 2 % smaller; not worth it |
| LERC lossless | 103.0 MB | Larger |
| LERC, 1 mm max error | 34.4 MB | About half; error far below DEM accuracy |
| LERC, 1 cm max error | 19.0 MB | About a quarter; needs a check that heights don't change |
| GeoPackage raster | 123.9 MB | Larger |

GeoPackage, SQLite and Parquet are table formats and don't help rasters.
LERC with a small max error is the realistic option. Test it by rerunning a
watershed and comparing heights before deciding.

## 8. Open questions (from `uv-run-2-plan.md` section 12)

1. Retry limit: 3 attempts while the service is healthy before giving up on a dam?
2. Half-flowline candidates: include them all if the dry run finds many?
3. The 835 dams without `Reach_ID`: fix in a separate task, or before the rerun?
