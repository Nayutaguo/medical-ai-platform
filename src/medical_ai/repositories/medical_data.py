from __future__ import annotations

from sqlalchemy import literal_column, select

from medical_ai.config import Settings, get_settings
from medical_ai.db import MySQLExecutor, QueryResult
from medical_ai.query import ExecutableQuery
from medical_ai.query.compiler import compile_statement


class MedicalDataRepository:
    """Provide database access without owning API or business rules."""

    def __init__(
        self,
        settings: Settings | None = None,
        executor: MySQLExecutor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.executor = executor or MySQLExecutor(settings=self.settings)

    def execute(self, query: ExecutableQuery) -> QueryResult:
        """Execute a previously validated and compiled read-only query."""

        return self.executor.execute(query)

    def check_connection(self) -> QueryResult:
        """Perform the cheapest possible MySQL readiness check.

        This deliberately avoids touching any medical-data table.  Keeping the
        probe as ``SELECT 1`` makes readiness independent of dataset size and
        prevents row counts or schema details from leaking through the health
        endpoint.
        """

        statement = select(literal_column("1").label("ok")).limit(1)
        sql, params = compile_statement(statement)
        query = ExecutableQuery(
            statement=statement,
            sql=sql,
            params=params,
            table="health_check",
            limit=1,
        )
        return self.executor.execute(query)

    def fetch_distinct_values(self, table: str, field: str, limit: int) -> QueryResult:
        """Read bounded distinct values from an allowlisted field."""

        return self.executor.fetch_distinct_values(table, field, limit)
