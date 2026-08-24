import json
from types import SimpleNamespace

import pytest

from medical_ai.agent.llm_client import LLMResponseError, LLMTimeoutError
from medical_ai.authorization import (
    AccessContext,
    EmptyFacilityScopeError,
    PermissionCode,
    PermissionDeniedError,
)
from medical_ai.config import Settings
from medical_ai.db import QueryResult
from medical_ai.query import PRIVACY_GROUP_COUNT_ALIAS, QueryValidationError
from medical_ai.services import AnalyticsService, UpstreamServiceError, UpstreamTimeoutError


class StubRepository:
    executor = None

    def __init__(self, execute_result: QueryResult | None = None) -> None:
        self.executed_queries = []
        self.distinct_requests = []
        self.execute_result = execute_result

    def execute(self, query):
        self.executed_queries.append(query)
        if self.execute_result is not None:
            return self.execute_result
        if query.limit == 1:
            return QueryResult(columns=["row_count"], rows=[{"row_count": 1000}], row_count=1, query_time_ms=2.0)
        if PRIVACY_GROUP_COUNT_ALIAS in query.sql:
            return QueryResult(
                columns=["AgeGroup", "patient_count", PRIVACY_GROUP_COUNT_ALIAS],
                rows=[
                    {
                        "AgeGroup": "50to69",
                        "patient_count": 12,
                        PRIVACY_GROUP_COUNT_ALIAS: 12,
                    }
                ],
                row_count=1,
                query_time_ms=3.0,
            )
        return QueryResult(
            columns=["AgeGroup", "patient_count"],
            rows=[{"AgeGroup": "50to69", "patient_count": 12}],
            row_count=1,
            query_time_ms=3.0,
        )

    def fetch_distinct_values(self, table: str, field: str, limit: int):
        self.distinct_requests.append((table, field, limit))
        return QueryResult(
            columns=[field],
            rows=[{field: "50to69"}],
            row_count=1,
            query_time_ms=1.0,
        )


def _service() -> AnalyticsService:
    settings = Settings(_env_file=None, mysql_database="medical_ai_test")
    return AnalyticsService(settings=settings, repository=StubRepository())


def _access_context(
    *,
    permissions: frozenset[PermissionCode] | None = None,
    facility_ids: frozenset[str] = frozenset({"facility-allowed"}),
) -> AccessContext:
    return AccessContext(
        user_id="user-1",
        organization_id="organization-1",
        membership_id="membership-1",
        permissions=(
            permissions
            if permissions is not None
            else frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE})
        ),
        allowed_facility_ids=facility_ids,
        identity_version=1,
        authorization_version=1,
    )


def _aggregate_query_spec() -> dict:
    return {
        "table": "inpatient",
        "select": ["AgeGroup"],
        "group_by": ["AgeGroup"],
        "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        "limit": 10,
    }


def test_health_is_dependency_free_and_non_sensitive() -> None:
    result = _service().health()

    assert result.data == {"status": "alive"}
    assert result.metrics == []


def test_readiness_uses_low_cost_connection_probe() -> None:
    repository = StubRepository()
    repository.check_connection = lambda: QueryResult(
        columns=["ok"],
        rows=[{"ok": 1}],
        row_count=1,
        query_time_ms=0.25,
    )
    service = AnalyticsService(settings=Settings(_env_file=None), repository=repository)

    result = service.readiness()

    assert result.data == {"status": "ready"}
    assert result.query_time_ms == 0.25


def test_public_query_requires_aggregate_metrics() -> None:
    with pytest.raises(QueryValidationError, match="仅允许返回聚合结果"):
        _service().query({"table": "inpatient", "select": ["AgeGroup"], "limit": 10})


def test_aggregate_query_returns_compilation_and_metadata() -> None:
    result = _service().query(_aggregate_query_spec())

    assert result.data["result"]["rows"][0]["patient_count"] == 12
    assert "SELECT" in result.data["compiled_sql"]
    assert "PermanentFacilityId" not in result.data["compiled_sql"]
    assert result.dimensions == ["AgeGroup"]
    assert result.metrics == ["patient_count"]


def test_authorized_query_injects_access_context_facility_scope() -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None, mysql_database="medical_ai_test"),
        repository=repository,
    )

    result = service.query_authorized(
        _aggregate_query_spec(),
        _access_context(facility_ids=frozenset({"facility-a", "facility-b"})),
    )

    assert result.data["result"]["row_count"] == 1
    compiled = repository.executed_queries[0]
    assert "`PermanentFacilityId` IN" in compiled.sql
    assert PRIVACY_GROUP_COUNT_ALIAS in compiled.sql
    assert {"facility-a", "facility-b"}.issubset(
        set(compiled.params.values())
    )
    assert "compiled_sql" not in result.data
    assert PRIVACY_GROUP_COUNT_ALIAS not in json.dumps(result.data)
    assert result.data["result"]["metadata"] == {
        "privacy_applied": True,
        "min_group_size": 5,
    }


@pytest.mark.parametrize(
    ("group_count", "expected_row_count"),
    [(5, 1), (4, 0)],
)
def test_authorized_global_aggregate_applies_minimum_group_privacy(
    group_count: int,
    expected_row_count: int,
) -> None:
    repository = StubRepository(
        execute_result=QueryResult(
            columns=["avg_total_charges", PRIVACY_GROUP_COUNT_ALIAS],
            rows=[
                {
                    "avg_total_charges": 12345.67,
                    PRIVACY_GROUP_COUNT_ALIAS: group_count,
                }
            ],
            row_count=1,
            query_time_ms=2.5,
        )
    )
    service = AnalyticsService(
        settings=Settings(_env_file=None, privacy_min_group_size=5),
        repository=repository,
    )
    result = service.query_authorized(
        {
            "table": "inpatient",
            "metrics": [
                {
                    "field": "TotalCharges",
                    "agg": "avg",
                    "alias": "avg_total_charges",
                }
            ],
        },
        _access_context(),
    )

    public_result = result.data["result"]
    assert public_result["row_count"] == expected_row_count
    assert "suppressed_group_count" not in public_result["metadata"]
    assert PRIVACY_GROUP_COUNT_ALIAS not in json.dumps(result.data)
    if expected_row_count == 0:
        assert public_result["rows"] == []
        assert "12345.67" not in json.dumps(result.data)


def test_authorized_grouped_query_keeps_safe_groups_and_suppresses_small_groups() -> None:
    suppressed_name = "suppressed-secret-category"
    repository = StubRepository(
        execute_result=QueryResult(
            columns=["AgeGroup", "patient_count", PRIVACY_GROUP_COUNT_ALIAS],
            rows=[
                {
                    "AgeGroup": "safe-category",
                    "patient_count": 8,
                    PRIVACY_GROUP_COUNT_ALIAS: 8,
                },
                {
                    "AgeGroup": suppressed_name,
                    "patient_count": 3,
                    PRIVACY_GROUP_COUNT_ALIAS: 3,
                },
            ],
            row_count=2,
            query_time_ms=3.0,
        )
    )
    service = AnalyticsService(
        settings=Settings(_env_file=None, privacy_min_group_size=5),
        repository=repository,
    )

    result = service.query_authorized(_aggregate_query_spec(), _access_context())

    public_result = result.data["result"]
    assert public_result["rows"] == [
        {"AgeGroup": "safe-category", "patient_count": 8}
    ]
    assert public_result["columns"] == ["AgeGroup", "patient_count"]
    assert "suppressed_group_count" not in public_result["metadata"]
    serialized = json.dumps(result.data)
    assert suppressed_name not in serialized
    assert PRIVACY_GROUP_COUNT_ALIAS not in serialized


def test_management_permission_does_not_grant_authorized_query_access() -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None, mysql_database="medical_ai_test"),
        repository=repository,
    )
    context = _access_context(permissions=frozenset({PermissionCode.USERS_MANAGE}))

    with pytest.raises(PermissionDeniedError):
        service.query_authorized(_aggregate_query_spec(), context)

    assert repository.executed_queries == []


def test_authorized_query_rejects_empty_facility_scope() -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None, mysql_database="medical_ai_test"),
        repository=repository,
    )
    context = _access_context(facility_ids=frozenset())

    with pytest.raises(EmptyFacilityScopeError):
        service.query_authorized(_aggregate_query_spec(), context)

    assert repository.executed_queries == []


def test_schema_authorized_requires_exact_schema_permission() -> None:
    service = _service()
    context = _access_context(
        permissions=frozenset({PermissionCode.ANALYTICS_SCHEMA_READ})
    )

    result = service.schema_authorized(context)

    assert result.data["tables"][0]["name"] == "inpatient"

    with pytest.raises(PermissionDeniedError):
        service.schema_authorized(
            _access_context(permissions=frozenset({PermissionCode.USERS_MANAGE}))
        )


def test_distinct_values_authorized_uses_only_scoped_execute_path() -> None:
    repository = StubRepository(
        execute_result=QueryResult(
            columns=["AgeGroup", PRIVACY_GROUP_COUNT_ALIAS],
            rows=[
                {"AgeGroup": "safe-category", PRIVACY_GROUP_COUNT_ALIAS: 7},
                {
                    "AgeGroup": "suppressed-secret-category",
                    PRIVACY_GROUP_COUNT_ALIAS: 2,
                },
            ],
            row_count=2,
            query_time_ms=1.5,
        )
    )
    service = AnalyticsService(
        settings=Settings(
            _env_file=None,
            mysql_database="medical_ai_test",
            query_max_distinct_values=25,
        ),
        repository=repository,
    )
    context = _access_context(
        permissions=frozenset({PermissionCode.ANALYTICS_DISTINCT_READ}),
        facility_ids=frozenset({"facility-a", "facility-b"}),
    )

    result = service.distinct_values_authorized(
        "inpatient",
        "AgeGroup",
        100,
        context,
    )

    assert result.data["values"] == ["safe-category"]
    assert repository.distinct_requests == []
    assert len(repository.executed_queries) == 1
    compiled = repository.executed_queries[0]
    assert compiled.limit == 25
    assert "`PermanentFacilityId` IN" in compiled.sql
    assert "GROUP BY inpatient.`AgeGroup`" in compiled.sql
    assert PRIVACY_GROUP_COUNT_ALIAS in compiled.sql
    assert {"facility-a", "facility-b"}.issubset(set(compiled.params.values()))
    assert result.data["metadata"]["privacy_applied"] is True
    assert result.data["metadata"]["min_group_size"] == 5
    assert "suppressed_group_count" not in result.data["metadata"]
    assert PRIVACY_GROUP_COUNT_ALIAS not in json.dumps(result.data)
    assert "suppressed-secret-category" not in json.dumps(result.data)


def test_no_rows_and_only_small_groups_have_identical_public_result() -> None:
    common_columns = ["AgeGroup", "patient_count", PRIVACY_GROUP_COUNT_ALIAS]
    empty_repository = StubRepository(
        execute_result=QueryResult(
            columns=common_columns,
            rows=[],
            row_count=0,
            query_time_ms=2.0,
            truncated=False,
            metadata={"table": "inpatient", "limit": 10},
        )
    )
    small_repository = StubRepository(
        execute_result=QueryResult(
            columns=common_columns,
            rows=[
                {
                    "AgeGroup": "suppressed-secret-category",
                    "patient_count": 2,
                    PRIVACY_GROUP_COUNT_ALIAS: 2,
                }
            ],
            row_count=1,
            query_time_ms=2.0,
            truncated=True,
            metadata={
                "table": "inpatient",
                "limit": 10,
                "suppressed_group_count": 1,
            },
        )
    )
    settings = Settings(_env_file=None, privacy_min_group_size=5)

    empty_result = AnalyticsService(
        settings=settings,
        repository=empty_repository,
    ).query_authorized(_aggregate_query_spec(), _access_context())
    small_result = AnalyticsService(
        settings=settings,
        repository=small_repository,
    ).query_authorized(_aggregate_query_spec(), _access_context())

    assert empty_result.data == small_result.data
    assert empty_result.data["result"]["rows"] == []
    assert empty_result.data["result"]["truncated"] is False
    assert "suppressed" not in json.dumps(empty_result.data)


def test_distinct_values_authorized_rejects_wrong_permission_and_empty_scope() -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None, mysql_database="medical_ai_test"),
        repository=repository,
    )

    with pytest.raises(PermissionDeniedError):
        service.distinct_values_authorized(
            "inpatient",
            "AgeGroup",
            10,
            _access_context(permissions=frozenset({PermissionCode.USERS_MANAGE})),
        )

    with pytest.raises(EmptyFacilityScopeError):
        service.distinct_values_authorized(
            "inpatient",
            "AgeGroup",
            10,
            _access_context(
                permissions=frozenset({PermissionCode.ANALYTICS_DISTINCT_READ}),
                facility_ids=frozenset(),
            ),
        )

    assert repository.distinct_requests == []
    assert repository.executed_queries == []


@pytest.mark.parametrize(
    "field",
    [
        "TotalCharges",
        "BirthWeight",
        "ZipCode3Digits",
        "CCSRDiagnosisDescription",
        "CCSRProcedureDescription",
        "PermanentFacilityId",
    ],
)
def test_distinct_values_authorized_rejects_sensitive_fields_before_execution(
    field: str,
) -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None),
        repository=repository,
    )
    context = _access_context(
        permissions=frozenset({PermissionCode.ANALYTICS_DISTINCT_READ})
    )

    with pytest.raises(QueryValidationError, match="distinct"):
        service.distinct_values_authorized("inpatient", field, 10, context)

    assert repository.distinct_requests == []
    assert repository.executed_queries == []


@pytest.mark.parametrize("aggregation", ["min", "max"])
def test_authorized_query_rejects_extreme_aggregations_before_execution(
    aggregation: str,
) -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None),
        repository=repository,
    )

    with pytest.raises(QueryValidationError, match="min/max"):
        service.query_authorized(
            {
                "table": "inpatient",
                "group_by": ["AgeGroup"],
                "metrics": [
                    {
                        "field": "TotalCharges",
                        "agg": aggregation,
                        "alias": "charge_extreme",
                    }
                ],
            },
            _access_context(),
        )

    assert repository.executed_queries == []


@pytest.mark.parametrize("field", ["ZipCode3Digits", "CCSRDiagnosisDescription"])
def test_authorized_query_rejects_unsafe_grouping_before_execution(
    field: str,
) -> None:
    repository = StubRepository()
    service = AnalyticsService(
        settings=Settings(_env_file=None),
        repository=repository,
    )

    with pytest.raises(QueryValidationError, match="分组或选择"):
        service.query_authorized(
            {
                "table": "inpatient",
                "group_by": [field],
                "metrics": [
                    {"field": "*", "agg": "count", "alias": "patient_count"}
                ],
            },
            _access_context(),
        )

    assert repository.executed_queries == []


def test_distinct_values_preserves_bounded_result_shape() -> None:
    result = _service().distinct_values("inpatient", "AgeGroup", 10)

    assert result.data["values"] == ["50to69"]
    assert result.dimensions == ["AgeGroup"]


def test_ask_authorized_resolves_scope_before_constructing_scoped_agent(
    monkeypatch,
) -> None:
    captured: dict = {}

    class CapturingAgent:
        def __init__(self, **kwargs) -> None:
            captured["kwargs"] = kwargs

        def run(self, question: str, execute: bool, interpret: bool):
            captured["run"] = (question, execute, interpret)

            class StubRun:
                plan = SimpleNamespace(query_spec=None)
                result = None

                def to_dict(self) -> dict:
                    return {
                        "raw_plan_response": "private",
                        "raw_insight_response": "private",
                    }

            return StubRun()

    monkeypatch.setattr("medical_ai.services.analytics.MedicalDataAgent", CapturingAgent)
    settings = Settings(
        _env_file=None,
        llm_base_url="https://llm.example/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    service = AnalyticsService(settings=settings, repository=StubRepository())
    context = _access_context(
        permissions=frozenset(
            {
                PermissionCode.ANALYTICS_AGENT_EXECUTE,
                PermissionCode.ANALYTICS_SCHEMA_READ,
                PermissionCode.ANALYTICS_DISTINCT_READ,
                PermissionCode.ANALYTICS_QUERY_EXECUTE,
            }
        ),
        facility_ids=frozenset({"facility-a", "facility-b"}),
    )

    result = service.ask_authorized("compare age groups", context, execute=False)

    assert captured["kwargs"]["trusted_facility_ids"] == frozenset(
        {"facility-a", "facility-b"}
    )
    assert captured["kwargs"]["require_aggregate"] is True
    assert captured["kwargs"]["privacy_min_group_size"] == 5
    assert captured["run"] == ("compare age groups", False, False)
    assert "raw_plan_response" not in result.data
    assert result.data["disclaimer"]


def test_ask_authorized_rejects_management_permission_and_empty_scope_before_agent(
    monkeypatch,
) -> None:
    constructed = False

    class UnexpectedAgent:
        def __init__(self, **kwargs) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr("medical_ai.services.analytics.MedicalDataAgent", UnexpectedAgent)
    settings = Settings(
        _env_file=None,
        llm_base_url="https://llm.example/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    service = AnalyticsService(settings=settings, repository=StubRepository())

    with pytest.raises(PermissionDeniedError):
        service.ask_authorized(
            "compare age groups",
            _access_context(permissions=frozenset({PermissionCode.USERS_MANAGE})),
        )

    with pytest.raises(EmptyFacilityScopeError):
        service.ask_authorized(
            "compare age groups",
            _access_context(
                permissions=frozenset(
                    {
                        PermissionCode.ANALYTICS_AGENT_EXECUTE,
                        PermissionCode.ANALYTICS_SCHEMA_READ,
                        PermissionCode.ANALYTICS_DISTINCT_READ,
                        PermissionCode.ANALYTICS_QUERY_EXECUTE,
                    }
                ),
                facility_ids=frozenset(),
            ),
        )

    assert constructed is False


def test_ask_authorized_agent_permission_alone_cannot_indirectly_read_data() -> None:
    service = AnalyticsService(
        settings=Settings(_env_file=None),
        repository=StubRepository(),
    )

    with pytest.raises(PermissionDeniedError):
        service.ask_authorized(
            "compare age groups",
            _access_context(
                permissions=frozenset({PermissionCode.ANALYTICS_AGENT_EXECUTE})
            ),
            execute=False,
        )


@pytest.mark.parametrize(
    "missing_permission",
    [
        PermissionCode.ANALYTICS_AGENT_EXECUTE,
        PermissionCode.ANALYTICS_SCHEMA_READ,
        PermissionCode.ANALYTICS_DISTINCT_READ,
        PermissionCode.ANALYTICS_QUERY_EXECUTE,
    ],
)
def test_ask_authorized_requires_every_agent_tool_permission_before_construction(
    monkeypatch,
    missing_permission: PermissionCode,
) -> None:
    constructed = False

    class UnexpectedAgent:
        def __init__(self, **kwargs) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr("medical_ai.services.analytics.MedicalDataAgent", UnexpectedAgent)
    settings = Settings(
        _env_file=None,
        llm_base_url="https://llm.example/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    service = AnalyticsService(settings=settings, repository=StubRepository())
    permissions = {
        PermissionCode.ANALYTICS_AGENT_EXECUTE,
        PermissionCode.ANALYTICS_SCHEMA_READ,
        PermissionCode.ANALYTICS_DISTINCT_READ,
        PermissionCode.ANALYTICS_QUERY_EXECUTE,
    }
    permissions.remove(missing_permission)

    with pytest.raises(PermissionDeniedError):
        service.ask_authorized(
            "compare age groups",
            _access_context(permissions=frozenset(permissions)),
            execute=False,
        )

    assert constructed is False


@pytest.mark.parametrize("privacy_min_group_size", [1, 101])
def test_privacy_min_group_size_setting_enforces_bounds(
    privacy_min_group_size: int,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            privacy_min_group_size=privacy_min_group_size,
        )


def test_privacy_min_group_size_setting_defaults_to_five() -> None:
    assert Settings(_env_file=None).privacy_min_group_size == 5


@pytest.mark.parametrize(
    ("agent_error", "expected_error"),
    [
        (LLMTimeoutError("timed out"), UpstreamTimeoutError),
        (LLMResponseError("empty response"), UpstreamServiceError),
    ],
)
def test_agent_transport_errors_are_mapped_to_stable_service_errors(
    monkeypatch,
    agent_error: Exception,
    expected_error: type[Exception],
) -> None:
    class FailingAgent:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, question: str, execute: bool, interpret: bool):
            raise agent_error

    monkeypatch.setattr("medical_ai.services.analytics.MedicalDataAgent", FailingAgent)
    settings = Settings(
        _env_file=None,
        llm_base_url="https://llm.example/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    service = AnalyticsService(settings=settings, repository=StubRepository())

    with pytest.raises(expected_error):
        service.ask("test question")
