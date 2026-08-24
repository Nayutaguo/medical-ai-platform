#!/usr/bin/env python
import argparse
import csv
import sys
import time
from pathlib import Path

import pymysql
from sqlalchemy import MetaData
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from medical_ai.config import get_settings
from medical_ai.data import CANONICAL_COLUMNS, cast_cleaned_row
from medical_ai.db import MySQLExecutor
from medical_ai.db.schema import build_sqlalchemy_table

DEFAULT_CLEAN_CSV = ROOT / "data" / "processed" / "inpatient_sparcs_2021_clean.csv"
COMMON_INDEXES = {
    "idx_inpatient_facility_year": "(`PermanentFacilityId`(128), `DischargeYear`)",
    "idx_inpatient_year_age": "(`DischargeYear`, `AgeGroup`(32))",
    "idx_inpatient_age": "(`AgeGroup`(32))",
    "idx_inpatient_gender": "(`Gender`(32))",
    "idx_inpatient_admission": "(`AdmissionType`(64))",
    "idx_inpatient_ccsr_diagnosis": "(`CCSRDiagnosisCode`(32))",
    "idx_inpatient_facility": "(`FacilityName`(128))",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Load cleaned SPARCS inpatient CSV into MySQL.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CLEAN_CSV)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--method", choices=["auto", "load-data", "insert"], default="auto")
    parser.add_argument("--replace", action="store_true", help="Delete rows before loading.")
    parser.add_argument("--replace-table", action="store_true", help="Drop and recreate inpatient table before loading.")
    parser.add_argument("--skip-indexes", action="store_true", help="Do not create common demo indexes after loading.")
    args = parser.parse_args()

    metadata = MetaData()
    table = build_sqlalchemy_table("inpatient", metadata)
    executor = MySQLExecutor()
    start = time.perf_counter()

    with executor.engine.begin() as connection:
        if args.replace_table:
            table.drop(connection, checkfirst=True)
            metadata.create_all(connection, tables=[table])
        else:
            metadata.create_all(connection, tables=[table])
        if args.replace:
            connection.execute(table.delete())

    loaded: int
    if args.method in {"auto", "load-data"} and args.limit is None:
        try:
            loaded = load_with_local_infile(args.csv)
        except Exception as exc:
            if args.method == "load-data":
                raise
            print(f"LOAD DATA LOCAL INFILE failed, falling back to batch inserts: {exc}", file=sys.stderr)
            loaded = load_with_inserts(args.csv, table, executor, args.batch_size, args.limit)
    else:
        loaded = load_with_inserts(args.csv, table, executor, args.batch_size, args.limit)

    if not args.skip_indexes:
        create_common_indexes(executor)

    elapsed = time.perf_counter() - start
    row_count = count_rows(executor)
    print(f"Loaded {loaded} cleaned SPARCS rows into inpatient in {elapsed:.2f}s.")
    print(f"inpatient row count is now {row_count}.")


def load_with_inserts(csv_path: Path, table, executor: MySQLExecutor, batch_size: int, limit: int | None) -> int:
    loaded = 0
    batch: list[dict] = []
    with executor.engine.begin() as connection:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                batch.append(cast_cleaned_row(row))
                loaded += 1
                if len(batch) >= batch_size:
                    connection.execute(table.insert(), batch)
                    batch.clear()
                if limit is not None and loaded >= limit:
                    break

        if batch:
            connection.execute(table.insert(), batch)

    return loaded


def load_with_local_infile(csv_path: Path) -> int:
    settings = get_settings()
    connection = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        local_infile=True,
        autocommit=False,
    )
    variables = ", ".join(f"@{column}" for column in CANONICAL_COLUMNS)
    assignments = ",\n  ".join(f"`{column}` = NULLIF(@{column}, '')" for column in CANONICAL_COLUMNS)
    sql = f"""
LOAD DATA LOCAL INFILE %s
INTO TABLE `inpatient`
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\\n'
IGNORE 1 LINES
({variables})
SET
  {assignments}
"""
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, (str(csv_path.resolve()),))
            loaded = cursor.rowcount
        connection.commit()
        return loaded
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_common_indexes(executor: MySQLExecutor) -> None:
    settings = executor.settings
    with executor.engine.begin() as connection:
        existing = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT INDEX_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = 'inpatient'
                    """
                ),
                {"schema_name": settings.mysql_database},
            )
        }
        for index_name, columns in COMMON_INDEXES.items():
            if index_name in existing:
                continue
            connection.execute(text(f"CREATE INDEX `{index_name}` ON `inpatient` {columns}"))


def count_rows(executor: MySQLExecutor) -> int:
    with executor.engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM inpatient")).scalar_one())


if __name__ == "__main__":
    main()
