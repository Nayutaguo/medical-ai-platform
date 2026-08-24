#!/usr/bin/env python
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
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
# 这些索引用于加速常见的分组、筛选和聚合查询。
COMMON_INDEXES = {
    "idx_inpatient_year_age": "(`DischargeYear`, `AgeGroup`(32))",
    "idx_inpatient_age": "(`AgeGroup`(32))",
    "idx_inpatient_gender": "(`Gender`(32))",
    "idx_inpatient_admission": "(`AdmissionType`(64))",
    "idx_inpatient_ccsr_diagnosis": "(`CCSRDiagnosisCode`(32))",
    "idx_inpatient_facility": "(`FacilityName`(128))",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Load cleaned SPARCS inpatient CSV into MySQL.")
    # --limit 适合开发测试；正式导入时不传该参数即可处理全部数据。
    parser.add_argument("--csv", type=Path, default=DEFAULT_CLEAN_CSV)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--method", choices=["auto", "load-data", "insert"], default="auto")
    parser.add_argument("--on-error", choices=["fail", "skip"], default="fail")
    parser.add_argument("--audit-output", type=Path, default=None)
    parser.add_argument("--replace", action="store_true", help="Delete rows before loading.")
    parser.add_argument("--replace-table", action="store_true", help="Drop and recreate inpatient table before loading.")
    parser.add_argument("--skip-indexes", action="store_true", help="Do not create common demo indexes after loading.")
    args = parser.parse_args()
    audit_output = args.audit_output or args.csv.with_suffix(".load.audit.json")
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "source_file": str(args.csv.resolve()),
        "target_table": "inpatient",
        "requested_method": args.method,
        "batch_size": args.batch_size,
        "on_error": args.on_error,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "batches": [],
    }

    metadata = MetaData()
    table = build_sqlalchemy_table("inpatient", metadata)
    executor = MySQLExecutor()
    start = time.perf_counter()

    # 根据参数初始化数据表。--replace-table 会删除旧表并按当前结构重建。
    with executor.engine.begin() as connection:
        if args.replace_table:
            table.drop(connection, checkfirst=True)
            metadata.create_all(connection, tables=[table])
        else:
            metadata.create_all(connection, tables=[table])
        if args.replace:
            connection.execute(table.delete())

    # 全量导入时优先使用 LOAD DATA；如果失败，auto 模式会自动退回分批 INSERT。
    loaded = 0
    rejected = 0
    row_count: int | None = None
    try:
        if args.method in {"auto", "load-data"} and args.limit is None:
            try:
                loaded = load_with_local_infile(args.csv)
                audit["method_used"] = "load-data"
                audit["batches"].append(
                    {
                        "batch_number": 1,
                        "attempted_rows": loaded,
                        "loaded_rows": loaded,
                        "rejected_rows": 0,
                        "errors": [],
                    }
                )
            except Exception as exc:
                if args.method == "load-data":
                    raise
                print(f"LOAD DATA LOCAL INFILE failed, falling back to batch inserts: {exc}", file=sys.stderr)
                audit["fallback_reason"] = str(exc)
                loaded, rejected = load_with_inserts(
                    args.csv, table, executor, args.batch_size, args.limit, audit, args.on_error
                )
                audit["method_used"] = "insert"
        else:
            loaded, rejected = load_with_inserts(
                args.csv, table, executor, args.batch_size, args.limit, audit, args.on_error
            )
            audit["method_used"] = "insert"

        if not args.skip_indexes:
            create_common_indexes(executor)

        row_count = count_rows(executor)
        elapsed = time.perf_counter() - start
        print(f"Loaded {loaded} cleaned SPARCS rows into inpatient in {elapsed:.2f}s.")
        print(f"inpatient row count is now {row_count}.")
    except Exception as exc:
        audit["run_error"] = str(exc)
        raise
    finally:
        audit.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.perf_counter() - start, 2),
                "attempted_rows": loaded + rejected,
                "loaded_rows": loaded,
                "rejected_rows": rejected,
                "database_row_count": row_count,
            }
        )
        audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote load audit to {audit_output}.")


def load_with_inserts(
    csv_path: Path,
    table,
    executor: MySQLExecutor,
    batch_size: int,
    limit: int | None,
    audit: dict,
    on_error: str,
) -> tuple[int, int]:
    loaded = 0
    rejected = 0
    batch_number = 0
    batch: list[dict] = []
    batch_errors: list[dict[str, object]] = []

    def flush_batch(connection) -> None:
        nonlocal batch_number, batch_errors
        if not batch and not batch_errors:
            return
        batch_number += 1
        if batch:
            connection.execute(table.insert(), batch)
        audit["batches"].append(
            {
                "batch_number": batch_number,
                "attempted_rows": len(batch) + len(batch_errors),
                "loaded_rows": len(batch),
                "rejected_rows": len(batch_errors),
                "errors": batch_errors[:50],
            }
        )
        batch.clear()
        batch_errors = []

    with executor.engine.begin() as connection:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_number, row in enumerate(reader, start=2):
                try:
                    # 先转换为与数据库字段类型匹配的值，再暂存在当前批次中。
                    batch.append(cast_cleaned_row(row))
                    loaded += 1
                except Exception as exc:
                    rejected += 1
                    batch_errors.append({"row_number": row_number, "error": str(exc)})
                    if on_error == "fail":
                        raise
                if len(batch) >= batch_size:
                    # 达到批次大小后一次性提交，减少逐行访问数据库的开销。
                    flush_batch(connection)
                if limit is not None and loaded >= limit:
                    break

        # 循环结束后处理不足一个完整批次的剩余记录和错误摘要。
        flush_batch(connection)

    return loaded, rejected


def load_with_local_infile(csv_path: Path) -> int:
    settings = get_settings()
    # LOAD DATA LOCAL INFILE 通常比逐批 INSERT 更快，适合全量文件导入。
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
    # 先读入用户变量，再把空字符串转换为 SQL NULL。
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
        # 导入失败时回滚当前事务，避免留下不完整的数据。
        connection.rollback()
        raise
    finally:
        connection.close()


def create_common_indexes(executor: MySQLExecutor) -> None:
    settings = executor.settings
    with executor.engine.begin() as connection:
        # 只创建不存在的索引，避免重复执行脚本时报错。
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
    # 最终查询数据库中的实际记录数，用于校验导入结果。
    with executor.engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM inpatient")).scalar_one())


if __name__ == "__main__":
    main()
