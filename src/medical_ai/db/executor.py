from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import Select, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from medical_ai.config import Settings, get_settings
from medical_ai.db.schema import build_sqlalchemy_table, get_column_spec
from medical_ai.query.compiler import ExecutableQuery, compile_statement

LOGGER = logging.getLogger(__name__)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    query_time_ms: float
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "query_time_ms": self.query_time_ms,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


class QueryExecutor(Protocol):
    def execute(self, query: ExecutableQuery) -> QueryResult:
        ...


class MySQLExecutor:
    def __init__(self, settings: Settings | None = None, engine: Engine | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or create_engine(
            self.settings.mysql_url(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10},
        )

    def execute(self, query: ExecutableQuery) -> QueryResult:
        statement = self._with_timeout_hint(query.statement)
        start = time.perf_counter()
        timeout_metadata = self._timeout_metadata()

        with self.engine.connect() as connection:
            if self.settings.query_timeout_ms > 0:
                try:
                    timeout_ms = int(self.settings.query_timeout_ms)
                    connection.execute(text(f"SET SESSION MAX_EXECUTION_TIME={timeout_ms}"))
                    timeout_metadata["mysql_session_timeout_set"] = True
                except SQLAlchemyError as exc:
                    timeout_metadata["mysql_session_timeout_set"] = False
                    LOGGER.warning(
                        "mysql_session_timeout_unavailable error_type=%s",
                        type(exc).__name__,
                    )

            result = connection.execute(statement)
            rows = [_jsonable_row(dict(row)) for row in result.mappings().all()]
            columns = list(result.keys())

        elapsed_ms = (time.perf_counter() - start) * 1000
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            query_time_ms=round(elapsed_ms, 3),
            truncated=len(rows) >= query.limit,
            metadata={
                "table": query.table,
                "limit": query.limit,
                **timeout_metadata,
            },
        )

    def fetch_distinct_values(self, table_name: str, field: str, limit: int) -> QueryResult:
        get_column_spec(table_name, field)
        effective_limit = min(max(limit, 1), self.settings.query_max_distinct_values)
        table = build_sqlalchemy_table(table_name)
        column = table.c[field]
        statement = select(column).select_from(table).where(column.is_not(None)).distinct().order_by(column.asc()).limit(
            effective_limit
        )
        sql, params = compile_statement(statement)
        query = ExecutableQuery(statement=statement, sql=sql, params=params, table=table_name, limit=effective_limit)
        return self.execute(query)

    def _with_timeout_hint(self, statement: Select[Any]) -> Select[Any]:
        if self.settings.query_timeout_ms <= 0:
            return statement
        timeout_ms = int(self.settings.query_timeout_ms)
        return statement.prefix_with(f"/*+ MAX_EXECUTION_TIME({timeout_ms}) */", dialect="mysql")

    def _timeout_metadata(self) -> dict[str, Any]:
        return {
            "query_timeout_ms": self.settings.query_timeout_ms,
            "mysql_timeout_hint_set": self.settings.query_timeout_ms > 0,
        }


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _jsonable_value(value) for key, value in row.items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
