from sqlalchemy.exc import SQLAlchemyError

from medical_ai.config import Settings
from medical_ai.db import MySQLExecutor
from medical_ai.query import compile_query


class FakeResult:
    def mappings(self):
        return self

    def all(self):
        return [{"patient_count": 1}]

    def keys(self):
        return ["patient_count"]


class FailingTimeoutConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement):
        if str(statement).startswith("SET SESSION"):
            raise SQLAlchemyError("secret SQL and binding detail")
        return FakeResult()


class FakeEngine:
    def connect(self):
        return FailingTimeoutConnection()


def test_timeout_setup_failure_never_leaks_database_exception(caplog) -> None:
    query = compile_query(
        {
            "table": "inpatient",
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
            "limit": 1,
        }
    )
    executor = MySQLExecutor(
        settings=Settings(_env_file=None, query_timeout_ms=1000),
        engine=FakeEngine(),
    )

    result = executor.execute(query)

    assert result.metadata["mysql_session_timeout_set"] is False
    assert "mysql_session_timeout_error" not in result.metadata
    assert "secret SQL and binding detail" not in str(result.to_dict())
    assert "secret SQL and binding detail" not in caplog.text
