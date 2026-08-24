from uuid import UUID

from sqlalchemy.exc import OperationalError

from medical_ai.api import create_app
from medical_ai.config import Settings
from medical_ai.db import QueryResult
from medical_ai.query import QueryValidationError
from medical_ai.repositories import MedicalDataRepository
from medical_ai.services import AnalyticsService, ServiceResult, UpstreamServiceError, UpstreamTimeoutError


class FakeAnalyticsService:
    def __init__(self) -> None:
        self.settings = Settings(_env_file=None, api_max_question_chars=20)
        self.liveness_calls = 0
        self.readiness_calls = 0

    def liveness(self) -> ServiceResult:
        self.liveness_calls += 1
        return ServiceResult(data={"status": "alive"})

    def readiness(self) -> ServiceResult:
        self.readiness_calls += 1
        return ServiceResult(data={"status": "ready"}, query_time_ms=1.5)

    def schema(self) -> ServiceResult:
        return ServiceResult(data={"tables": []})

    def distinct_values(self, table: str, field: str, limit: int) -> ServiceResult:
        return ServiceResult(data={"table": table, "field": field, "values": [], "row_count": 0})

    def query(self, query_spec: dict) -> ServiceResult:
        if query_spec.get("table") == "invalid":
            raise QueryValidationError("invalid query")
        return ServiceResult(data={"result": {"rows": []}})

    def ask(self, question: str, execute: bool = True) -> ServiceResult:
        if question == "timeout":
            raise UpstreamTimeoutError("上游模型服务响应超时，请稍后重试")
        if question == "upstream":
            raise UpstreamServiceError("上游模型服务返回异常，请稍后重试")
        return ServiceResult(data={"question": question, "execute": execute})


def _client(service=None):
    settings = Settings(_env_file=None, api_max_question_chars=20)
    app = create_app(settings=settings, analytics_service=service or FakeAnalyticsService())
    app.config["TESTING"] = True
    return app.test_client()


def test_health_alias_is_non_sensitive_liveness_with_request_id() -> None:
    service = FakeAnalyticsService()
    response = _client(service).get("/api/v1/health", headers={"X-Request-Id": "trace-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "trace-123"
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"] == {"status": "alive"}
    assert payload["meta"]["request_id"] == "trace-123"
    assert payload["meta"]["metrics"] == []
    assert payload["error"] is None
    assert service.liveness_calls == 1
    assert service.readiness_calls == 0

    serialized = response.get_data(as_text=True).lower()
    for forbidden in ("row_count", "database", "mysql_database", "llm_configured"):
        assert forbidden not in serialized


def test_live_probe_does_not_call_repository() -> None:
    class ExplodingRepository:
        executor = None

        def check_connection(self):
            raise AssertionError("liveness must not touch the repository")

    settings = Settings(_env_file=None)
    service = AnalyticsService(settings=settings, repository=ExplodingRepository())

    response = _client(service).get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.get_json()["data"] == {"status": "alive"}


def test_ready_probe_uses_service_and_returns_no_dependency_details() -> None:
    service = FakeAnalyticsService()

    response = _client(service).get("/api/v1/health/ready")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"] == {"status": "ready"}
    assert payload["meta"]["metrics"] == []
    assert service.readiness_calls == 1
    serialized = response.get_data(as_text=True).lower()
    for forbidden in ("row_count", "database", "mysql_database", "llm_configured"):
        assert forbidden not in serialized


def test_repository_readiness_probe_is_select_one_not_table_scan() -> None:
    class CapturingExecutor:
        def __init__(self) -> None:
            self.query = None

        def execute(self, query):
            self.query = query
            return QueryResult(
                columns=["ok"],
                rows=[{"ok": 1}],
                row_count=1,
                query_time_ms=0.5,
            )

    executor = CapturingExecutor()
    repository = MedicalDataRepository(
        settings=Settings(_env_file=None),
        executor=executor,
    )

    result = repository.check_connection()

    normalized_sql = " ".join(executor.query.sql.split()).upper()
    assert normalized_sql.startswith("SELECT 1 AS OK")
    assert "COUNT(" not in normalized_sql
    assert "INPATIENT" not in normalized_sql
    assert result.rows == [{"ok": 1}]


def test_ready_probe_maps_database_failure_to_safe_503(caplog) -> None:
    class UnavailableService(FakeAnalyticsService):
        def readiness(self) -> ServiceResult:
            raise OperationalError("SELECT 1", {}, RuntimeError("secret connection detail"))

    response = _client(UnavailableService()).get("/api/v1/health/ready")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["error"] == {
        "code": "DATABASE_UNAVAILABLE",
        "message": "数据服务暂时不可用，请稍后重试",
    }
    assert "secret connection detail" not in response.get_data(as_text=True)
    assert "secret connection detail" not in caplog.text


def test_invalid_request_id_is_replaced_with_uuid() -> None:
    response = _client().get("/api/v1/schema", headers={"X-Request-Id": "bad request id"})

    request_id = response.headers["X-Request-Id"]
    assert str(UUID(request_id)) == request_id


def test_missing_question_returns_stable_field_error() -> None:
    response = _client().post("/api/v1/ask", json={})

    payload = response.get_json()
    assert response.status_code == 400
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "INVALID_REQUEST"
    assert payload["error"]["field_errors"][0]["field"] == "question"


def test_non_json_query_is_rejected() -> None:
    response = _client().post("/api/v1/query", data="not-json", content_type="text/plain")

    assert response.status_code == 415
    assert response.get_json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_query_validation_error_is_mapped_to_400() -> None:
    response = _client().post("/api/v1/query", json={"table": "invalid"})

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_QUERY_SPEC"


def test_unknown_api_route_returns_json_404() -> None:
    response = _client().get("/api/v1/not-found")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "NOT_FOUND"


def test_upstream_timeout_is_mapped_to_json_504() -> None:
    response = _client().post("/api/v1/ask", json={"question": "timeout"})

    assert response.status_code == 504
    assert response.content_type == "application/json"
    assert response.get_json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_upstream_response_error_is_mapped_to_json_502() -> None:
    response = _client().post("/api/v1/ask", json={"question": "upstream"})

    assert response.status_code == 502
    assert response.content_type == "application/json"
    assert response.get_json()["error"]["code"] == "UPSTREAM_SERVICE_ERROR"
