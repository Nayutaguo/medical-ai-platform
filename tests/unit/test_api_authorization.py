from __future__ import annotations

from datetime import datetime, timedelta

from medical_ai.api import create_app
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.config import Settings
from medical_ai.identity.errors import AuthenticationRequiredError, CsrfValidationError
from medical_ai.identity.models import ActiveSession
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.history import HistoryError
from medical_ai.services import ServiceResult


class GovernedAnalyticsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple] = []

    def liveness(self) -> ServiceResult:
        self.calls.append(("liveness",))
        return ServiceResult(data={"status": "alive"})

    def schema_authorized(self, context: AccessContext) -> ServiceResult:
        self.calls.append(("schema_authorized", context))
        return ServiceResult(data={"tables": []})

    def distinct_values_authorized(
        self,
        table: str,
        field: str,
        limit: int,
        context: AccessContext,
    ) -> ServiceResult:
        self.calls.append(("distinct_authorized", table, field, limit, context))
        return ServiceResult(data={"values": []})

    def query_authorized(self, query_spec: dict, context: AccessContext) -> ServiceResult:
        self.calls.append(("query_authorized", query_spec, context))
        return ServiceResult(data={"result": {"rows": []}})

    def ask_authorized(
        self,
        question: str,
        context: AccessContext,
        execute: bool = True,
    ) -> ServiceResult:
        self.calls.append(("ask_authorized", question, execute, context))
        return ServiceResult(data={"question": question})

    def schema(self):
        raise AssertionError("legacy schema path must not be used")

    def distinct_values(self, *_args):
        raise AssertionError("legacy distinct path must not be used")

    def query(self, *_args):
        raise AssertionError("legacy query path must not be used")

    def ask(self, *_args, **_kwargs):
        raise AssertionError("legacy ask path must not be used")


class GovernedAuthenticationService:
    def __init__(self) -> None:
        self.session_token = "opaque-session-token"
        self.csrf_token = "csrf-proof"
        self.active = _active_session()
        self.resolve_calls: list[str] = []
        self.csrf_calls: list[tuple[ActiveSession, str]] = []

    def resolve_session(self, token: str) -> ActiveSession:
        self.resolve_calls.append(token)
        if token != self.session_token:
            raise AuthenticationRequiredError
        return self.active

    def require_csrf(self, session: ActiveSession, token: str) -> None:
        self.csrf_calls.append((session, token))
        if token != self.csrf_token:
            raise CsrfValidationError


class GovernedHistoryService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.failure: HistoryError | None = None

    def record_query(
        self,
        context: AccessContext,
        query_spec: dict,
        *,
        result_data: dict,
        query_time_ms: float | None,
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            ("record_query", context, query_spec, result_data, query_time_ms)
        )

    def record_agent(
        self,
        context: AccessContext,
        question: str,
        *,
        result_data: dict,
        query_time_ms: float | None,
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            ("record_agent", context, question, result_data, query_time_ms)
        )


def _active_session() -> ActiveSession:
    context = AccessContext(
        user_id="user-1",
        organization_id="organization-1",
        membership_id="membership-1",
        permissions=frozenset(PermissionCode),
        allowed_facility_ids=frozenset({"facility-1"}),
        identity_version=1,
        authorization_version=1,
    )
    now = datetime(2026, 8, 24, 10, 0)
    return ActiveSession(
        session_id="session-1",
        user_id="user-1",
        email="analyst@example.com",
        display_name="Analyst",
        organization_name="Hospital A",
        expires_at=now + timedelta(hours=12),
        idle_expires_at=now + timedelta(minutes=30),
        csrf_token_hash=hash_opaque_token("csrf-proof"),
        access_context=context,
    )


def _client():
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        auth_session_cookie_name="test_session",
    )
    analytics = GovernedAnalyticsService(settings)
    authentication = GovernedAuthenticationService()
    history = GovernedHistoryService()
    app = create_app(
        settings=settings,
        analytics_service=analytics,
        authentication_service=authentication,
        history_service=history,
    )
    app.config["TESTING"] = True
    return app.test_client(), analytics, authentication, history


def _authenticate(client, authentication: GovernedAuthenticationService) -> None:
    client.set_cookie("test_session", authentication.session_token, path="/api/v1")


def test_governed_schema_requires_session_and_never_calls_legacy_path() -> None:
    client, analytics, authentication, history = _client()

    denied = client.get("/api/v1/schema")
    assert denied.status_code == 401
    assert analytics.calls == []

    _authenticate(client, authentication)
    allowed = client.get("/api/v1/schema")
    assert allowed.status_code == 200
    assert analytics.calls[0][0] == "schema_authorized"
    assert history.calls == []


def test_governed_query_requires_csrf_before_authorized_service() -> None:
    client, analytics, authentication, history = _client()
    _authenticate(client, authentication)
    payload = {
        "table": "inpatient",
        "group_by": ["AgeGroup"],
        "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
    }

    denied = client.post("/api/v1/query", json=payload)
    assert denied.status_code == 403
    assert denied.headers["Cache-Control"] == "no-store"
    assert denied.headers["Pragma"] == "no-cache"
    assert denied.get_json()["error"]["code"] == "CSRF_VALIDATION_FAILED"
    assert analytics.calls == []
    assert history.calls == []

    allowed = client.post(
        "/api/v1/query",
        json=payload,
        headers={"X-CSRF-Token": authentication.csrf_token},
    )
    assert allowed.status_code == 200
    assert allowed.headers["Cache-Control"] == "no-store"
    assert allowed.headers["Pragma"] == "no-cache"
    assert analytics.calls[0][0] == "query_authorized"
    assert history.calls[0][0] == "record_query"
    assert history.calls[0][2] == payload


def test_governed_distinct_and_agent_use_authorized_services() -> None:
    client, analytics, authentication, history = _client()
    _authenticate(client, authentication)

    distinct = client.get(
        "/api/v1/distinct?table=inpatient&field=AgeGroup&limit=10"
    )
    ask = client.post(
        "/api/v1/ask",
        json={"question": "按年龄组统计患者数量"},
        headers={"X-CSRF-Token": authentication.csrf_token},
    )

    assert distinct.status_code == 200
    assert ask.status_code == 200
    assert [call[0] for call in analytics.calls] == [
        "distinct_authorized",
        "ask_authorized",
    ]
    assert [call[0] for call in history.calls] == ["record_agent"]


def test_history_persistence_failure_does_not_discard_a_completed_query() -> None:
    client, analytics, authentication, history = _client()
    history.failure = HistoryError("history unavailable")
    _authenticate(client, authentication)

    response = client.post(
        "/api/v1/query",
        json={
            "table": "inpatient",
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        },
        headers={"X-CSRF-Token": authentication.csrf_token},
    )

    assert response.status_code == 200
    assert analytics.calls[0][0] == "query_authorized"


def test_health_remains_public_when_governance_is_enabled() -> None:
    client, analytics, authentication, history = _client()

    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert analytics.calls == [("liveness",)]
    assert authentication.resolve_calls == []
    assert history.calls == []
