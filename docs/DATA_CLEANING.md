# Data cleaning and full-data publish

The governed SPARCS 2021 source is outside the repository:

```text
../医养项目数据/Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv/Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv
```

Observed source facts on 2026-08-25:

- size: 832,373,138 bytes (about 794 MiB);
- data rows: 2,101,588, plus one header row;
- raw columns: 33;
- SHA-256: `185808e20900c0499f7974d5ac9c05f0909df506bc088a244443bff895ca2219`.

Raw, cleaned, profile, manifest, load-audit, database-export, and backup files are
not Git artifacts. `data/processed/*` and the parent data directory are ignored
and must remain outside commits and logs.

## Full acceptance path

Run the streaming cleaner without `--limit`:

```bash
conda run -n medical-ai python scripts/clean_sparcs_csv.py \
  --input "../医养项目数据/Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv/Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv" \
  --output data/processed/inpatient_sparcs_2021_clean.csv
```

The cleaner writes three private artifacts in the same directory:

```text
inpatient_sparcs_2021_clean.csv
inpatient_sparcs_2021_clean.profile.json
inpatient_sparcs_2021_clean.manifest.json
```

The manifest is the only accepted commit point for a live import. It includes
the cleaning task ID, schema version, source/output hashes, input/output/rejected
rows, quality gates, timestamps, and a stable error. A successful full manifest
must have `source_complete=true`; a development `--limit` run cannot publish the
live table.

Validate the clean artifact and MySQL state without creating or renaming tables:

```bash
conda run -n medical-ai python scripts/load_sparcs_mysql.py \
  --csv data/processed/inpatient_sparcs_2021_clean.csv \
  --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json \
  --dry-run
```

Publish through an isolated staging table:

```bash
conda run -n medical-ai python scripts/load_sparcs_mysql.py \
  --csv data/processed/inpatient_sparcs_2021_clean.csv \
  --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json \
  --method auto
```

`auto` first tries `LOAD DATA LOCAL INFILE` and falls back to bounded batch
inserts only when local infile is unavailable. To require a specific path, use
`--method load-data` or `--method insert`. Enabling `local_infile` is an explicit
database-administration action; do not silently change server configuration in
the loader.

The importer:

1. validates the successful, complete cleaning manifest;
2. verifies canonical header, file size, SHA-256, and path separation, then binds
   loading to a task-private hard-link snapshot of those accepted bytes;
3. acquires the database dataset-publish lock;
4. creates a canonical task-specific staging table whose ownership marker is
   part of the same `CREATE TABLE` statement;
5. rejects MySQL conversion warnings and reconciles staging rows;
6. validates the non-newborn birth-weight rule and required indexes;
7. rechecks the snapshot stat/SHA-256 and verifies the original live-table
   identity before publication;
8. atomically renames staging to live while retaining the old live as backup;
9. reconciles live/backup identity through table markers, row counts, comments,
   and schema fingerprints on a new connection;
10. appends full snapshots to a task-specific, non-overwriting JSON Lines audit.

Never use a fixed audit path shared by multiple attempts. The default audit
name contains the generated task ID and ends in `.load.audit.jsonl`. Each line
is a complete audit snapshot; a crash may leave only the last line incomplete,
while previous newline-terminated checkpoints remain recoverable. The path must
not equal the CSV, manifest, profile, or task snapshot path.

The old live table is retained by default. Deleting a retained backup is a
separate, approved maintenance action after application verification and backup
retention review; the import command does not combine data publication and
rollback-copy deletion.

## Development subset

A subset is useful for cleaner development only:

```bash
conda run -n medical-ai python scripts/clean_sparcs_csv.py \
  --limit 1000 \
  --output data/processed/inpatient_sparcs_2021_clean_1000.csv
```

Its manifest records `source_complete=false`. It may be inspected and used in
isolated tests, but the production loader rejects it as a replacement for
`inpatient`. Use `scripts/load_sample_mysql.py` for the explicit synthetic demo
database instead of weakening the full-data quality gate.

## Normalization rules

- `Age Group`:
  - `0 to 17` -> `0to17`
  - `18 to 29` -> `18to29`
  - `30 to 49` -> `30to49`
  - `50 to 69` -> `50to69`
  - `70 or Older` -> `70orOlder`
- `Gender`: `F` -> `Female`, `M` -> `Male`, `U` -> `Unknown`.
- `Emergency Department Indicator`: `Y` -> `Yes`, `N` -> `No`.
- `Length of Stay` values such as `120 +` are stored as integer `120`.
- `Total Charges` and `Total Costs` remove separators and are stored as decimal.
- Empty/missing markers become SQL `NULL`.
- `BirthWeight` is retained only for `AdmissionType=Newborn`; all other values
  are set to `NULL`.
- `RaceEthnicity` is derived as `Race | Ethnicity` for the existing governed
  QuerySpec field.

The source has no documented stable patient/business key, so the current
pipeline does not silently deduplicate identical rows. A future deduplication
rule requires a reviewed business key and keep policy.

## Verified local result

The 2026-08-25 local acceptance produced:

- cleaned rows: 2,101,588;
- rejected rows: 0;
- cleaned SHA-256:
  `d815d5d9b64680b3308636e408f4b291e2711918787a1cfd1d4a58a3635456e0`;
- MySQL live rows: 2,101,588;
- non-newborn rows with non-null birth weight: 0;
- distinct non-null facility IDs: 205;
- rows with missing facility ID: 10,642;
- retained rollback table:
  `inpatient_backup_876dcce267b14044bfe85e5270b6207f` (1,000 rows).

Missing-facility records remain in the accepted dataset for quality visibility,
but fail closed from facility-scoped product queries. They must not be assigned
to an organization without an approved mapping rule.

## Remaining product work

The local CLI proves the data path, but the final product still needs:

- durable `background_jobs` execution with heartbeat, cancellation, retry, and
  checkpoint/resume;
- `dataset_versions` activation/supersession and organization ownership;
- rejected-row artifacts and reviewed quality thresholds for future sources;
- a data-provider-approved business key and duplicate policy;
- facility catalog/mapping governance for missing and new facility IDs;
- concurrency/load, backup/restore, and interrupted-import drills in a production-
  like environment.

See [ADR 0004](adr/0004-recoverable-full-data-publish.md) for the publication and
recovery decision.
