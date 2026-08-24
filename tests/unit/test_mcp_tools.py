import asyncio

import pytest

from medical_ai.mcp_server.server import mcp
from medical_ai.config import Settings
from medical_ai.mcp_server.tools import get_database_schema, query_medical_data, validate_query_spec_only
from medical_ai.query import QueryValidationError


def test_get_database_schema_is_available_without_database() -> None:
    schema = get_database_schema()
    table = schema["tables"][0]

    assert table["name"] == "inpatient"
    assert any(column["name"] == "TotalCharges" for column in table["columns"])


def test_validate_query_spec_only_reports_invalid_query_without_database() -> None:
    result = validate_query_spec_only(
        {
            "table": "inpatient",
            "filters": [{"field": "NotAField", "op": "=", "value": 1}],
            "limit": 10,
        }
    )

    assert result["valid"] is False
    assert "NotAField" in result["error"]


def test_mcp_server_registers_expected_tools() -> None:
    tool_names = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert {"get_database_schema", "get_distinct_values", "query_medical_data"}.issubset(tool_names)


def test_mcp_query_rejects_patient_level_results_before_database_access() -> None:
    with pytest.raises(QueryValidationError, match="仅允许返回聚合结果"):
        query_medical_data({"table": "inpatient", "select": ["AgeGroup"], "limit": 10})


def test_unscoped_mcp_tools_can_be_disabled_fail_closed(monkeypatch) -> None:
    settings = Settings(_env_file=None, mcp_allow_unscoped_tools=False)
    monkeypatch.setattr("medical_ai.mcp_server.tools.get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="authenticated AccessContext"):
        get_database_schema()
