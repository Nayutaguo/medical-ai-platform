from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from medical_ai.api import create_app
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.config import Settings
from medical_ai.history import (
    HistoryEntry,
    HistoryPage,
    HistoryType,
    HistoryVersionConflictError,
)
from medical_ai.identity.errors import AuthenticationRequiredError, CsrfValidationError
from medical_ai.identity.models import ActiveSession
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services import ServiceResult


class AuthenticationService:
    session_token = "opaque-session-token"
    csrf_token = "csrf-proof"

    def __init__(self) -> None:
        self.active = _active_session()

    def resolve_session(self, token: str) -> ActiveSession:
        if token != self.session_token:
            raise AuthenticationRequiredError
        return self.active

    def require_csrf(self, session: ActiveSession, token: str) -> None:
        if token != self.csrf_token:
            raise CsrfValidationError


class HistoryService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.entry = _entry()

    def list_history(self, context, **kwargs):
        self.calls.append(("list", context, kwargs))
        return HistoryPage(items=(self.entry,), next_cursor="1")

    def set_favorite(self, context, history_id, **kwargs):
        self.calls.append(("favorite", context, history_id, kwargs))
        self.entry = replace(
            self.entry,
            is_favorite=kwargs["is_favorite"],
            version=self.entry.version + 1,
        )
        return self.entry

    def delete_history(self, context, history_id):
        self.calls.append(("delete", context, history_id))


def test_history_list_is_authenticated_bounded_and_no_store() -> None:
    client, history, authentication = _client()

    denied = client.get("/api/v1/history")
    assert denied.status_code == 401

    _authenticate(client, authentication)
    response = client.get("/api/v1/history?cursor=9&limit=10&favorite=true")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    payload = response.get_json()
    assert payload["data"]["items"][0]["id"] == "1"
    assert payload["data"]["items"][0]["kind"] == "query"
    assert payload["data"]["next_cursor"] == "1"
    assert history.calls[0][2] == {"cursor": 9, "limit": 10, "favorite": True}
    assert "organization_id" not in payload["data"]["items"][0]
    assert "membership_id" not in payload["data"]["items"][0]


def test_favorite_and_delete_require_csrf_and_support_version_alias() -> None:
    client, history, authentication = _client()
    _authenticate(client, authentication)

    denied = client.patch(
        "/api/v1/history/1/favorite",
        json={"is_favorite": True, "version": 1},
    )
    assert denied.status_code == 403
    assert history.calls == []

    favorite = client.patch(
        "/api/v1/history/1/favorite",
        json={"is_favorite": True, "expected_version": 1},
        headers={"X-CSRF-Token": authentication.csrf_token},
    )
    deleted = client.delete(
        "/api/v1/history/1",
        headers={"X-CSRF-Token": authentication.csrf_token},
    )

    assert favorite.status_code == 200
    assert favorite.get_json()["data"]["is_favorite"] is True
    assert favorite.get_json()["data"]["version"] == 2
    assert deleted.status_code == 200
    assert deleted.get_json()["data"] == {"deleted": True, "id": "1"}
    assert [call[0] for call in history.calls] == ["favorite", "delete"]


def test_history_validation_and_version_conflict_have_stable_errors() -> None:
    client, history, authentication = _client()
    _authenticate(client, authentication)

    invalid = client.get("/api/v1/history?limit=101")
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "INVALID_HISTORY_REQUEST"

    def conflict(*_args, **_kwargs):
        raise HistoryVersionConflictError("stale")

    history.set_favorite = conflict
    response = client.patch(
        "/api/v1/history/1/favorite",
        json={"is_favorite": True, "version": 1},
        headers={"X-CSRF-Token": authentication.csrf_token},
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "HISTORY_VERSION_CONFLICT"


def test_anonymous_demo_query_does_not_record_history() -> None:
    history = HistoryService()

    class AnonymousAnalytics:
        settings = Settings(_env_file=None, auth_enforcement_enabled=False)

        def query(self, query_spec):
            return ServiceResult(data={"result": {"rows": [], "row_count": 0}})

    app = create_app(
        settings=AnonymousAnalytics.settings,
        analytics_service=AnonymousAnalytics(),
        history_service=history,
    )
    app.config["TESTING"] = True

    response = app.test_client().post("/api/v1/query", json={"table": "inpatient"})

    assert response.status_code == 200
    assert history.calls == []


def _client():
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        auth_session_cookie_name="test_session",
    )
    authentication = AuthenticationService()
    history = HistoryService()
    app = create_app(
        settings=settings,
        analytics_service=object(),
        authentication_service=authentication,
        history_service=history,
    )
    app.config["TESTING"] = True
    return app.test_client(), history, authentication


def _authenticate(client, authentication: AuthenticationService) -> None:
    client.set_cookie("test_session", authentication.session_token, path="/api/v1")


def _active_session() -> ActiveSession:
    context = AccessContext(
        user_id="user-1",
        organization_id="organization-1",
        membership_id="membership-1",
        permissions=frozenset(
            {
                PermissionCode.ANALYTICS_QUERY_EXECUTE,
                PermissionCode.ANALYTICS_AGENT_EXECUTE,
            }
        ),
        allowed_facility_ids=frozenset({"facility-1"}),
        identity_version=1,
        authorization_version=1,
    )
    now = datetime(2026, 8, 26, 10, 0)
    return ActiveSession(
        session_id="session-1",
        user_id=context.user_id,
        email="analyst@example.com",
        display_name="Analyst",
        organization_name="Hospital A",
        expires_at=now + timedelta(hours=12),
        idle_expires_at=now + timedelta(minutes=30),
        csrf_token_hash=hash_opaque_token(AuthenticationService.csrf_token),
        access_context=context,
    )


def _entry() -> HistoryEntry:
    now = datetime(2026, 8, 26, 12, 0)
    return HistoryEntry(
        id=1,
        history_type=HistoryType.QUERY,
        title="AgeGroup · patient_count",
        question=None,
        query_spec={"table": "inpatient"},
        chart_spec=None,
        row_count=2,
        truncated=False,
        query_time_ms=1.25,
        is_favorite=False,
        created_at=now,
        updated_at=now,
        version=1,
    )
