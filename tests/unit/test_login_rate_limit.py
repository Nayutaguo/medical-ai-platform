from __future__ import annotations

from typing import Any

import pytest

from medical_ai.api import create_app
from medical_ai.config import Settings
from medical_ai.rate_limit import (
    LoginRateLimitExceeded,
    LoginRateLimitUnavailable,
    RedisLoginRateLimiter,
)
from medical_ai.services import ServiceResult


SECRET = "unit-test-rate-limit-secret-32-bytes-minimum"


class ScriptRedis:
    """Small deterministic stand-in for the two-key Lua contract."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.calls: list[tuple[Any, ...]] = []

    def eval(self, script, number_of_keys, *arguments):
        self.calls.append((script, number_of_keys, *arguments))
        ip_key, account_key, ip_limit, account_limit, window_ms = arguments
        self.counts[ip_key] = self.counts.get(ip_key, 0) + 1
        self.counts[account_key] = self.counts.get(account_key, 0) + 1
        allowed = (
            self.counts[ip_key] <= int(ip_limit)
            and self.counts[account_key] <= int(account_limit)
        )
        return [1, 0] if allowed else [0, int(window_ms)]


class BrokenRedis:
    def eval(self, *_args, **_kwargs):
        raise ConnectionError("synthetic Redis outage")


class MinimalAnalyticsService:
    def liveness(self) -> ServiceResult:
        return ServiceResult(data={"status": "alive"})


class AuthenticationMustNotRun:
    def __init__(self) -> None:
        self.login_calls = 0

    def login(self, *_args, **_kwargs):
        self.login_calls += 1
        raise AssertionError("password verification must not run after limiter rejection")


class RegistrationMustNotRun:
    def __init__(self) -> None:
        self.registration_calls = 0

    def register_invited_user(self, **_kwargs):
        self.registration_calls += 1
        raise AssertionError("password hashing must not run after limiter rejection")


class RejectingLimiter:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception
        self.calls: list[dict[str, str]] = []

    def check(self, *, source_ip: str, normalized_account: str) -> None:
        self.calls.append(
            {"source_ip": source_ip, "normalized_account": normalized_account}
        )
        raise self.exception


def _redis_limiter(redis_client, *, ip_limit=2, account_limit=2):
    return RedisLoginRateLimiter(
        redis_client,
        key_secret=SECRET,
        ip_attempt_limit=ip_limit,
        account_attempt_limit=account_limit,
        window_seconds=60,
    )


def _api_client(limiter):
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
    )
    authentication = AuthenticationMustNotRun()
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(),
        authentication_service=authentication,
        login_rate_limiter=limiter,
    )
    app.config["TESTING"] = True
    return app.test_client(), authentication


def test_redis_limiter_atomically_uses_two_hmac_only_keys() -> None:
    redis_client = ScriptRedis()
    limiter = _redis_limiter(redis_client)

    limiter.check(
        source_ip="2001:0db8:0:0:0:0:0:1",
        normalized_account="Analyst@Example.com",
    )

    call = redis_client.calls[0]
    assert call[1] == 2
    ip_key, account_key = call[2], call[3]
    assert ip_key.startswith("medical-ai:rate-limit:login:ip:")
    assert account_key.startswith("medical-ai:rate-limit:login:account:")
    serialized_keys = f"{ip_key} {account_key}"
    assert "2001:" not in serialized_keys
    assert "analyst" not in serialized_keys.casefold()
    assert len(ip_key.rsplit(":", 1)[1]) == 64
    assert len(account_key.rsplit(":", 1)[1]) == 64


def test_ip_bucket_blocks_across_different_accounts() -> None:
    limiter = _redis_limiter(ScriptRedis(), ip_limit=1, account_limit=10)
    limiter.check(source_ip="192.0.2.10", normalized_account="one@example.com")

    with pytest.raises(LoginRateLimitExceeded) as exc_info:
        limiter.check(source_ip="192.0.2.10", normalized_account="two@example.com")

    assert exc_info.value.retry_after_seconds == 60


def test_account_bucket_blocks_across_different_source_addresses() -> None:
    limiter = _redis_limiter(ScriptRedis(), ip_limit=10, account_limit=1)
    limiter.check(source_ip="192.0.2.10", normalized_account="one@example.com")

    with pytest.raises(LoginRateLimitExceeded):
        limiter.check(source_ip="192.0.2.11", normalized_account="one@example.com")


def test_redis_failure_is_a_fail_closed_domain_error() -> None:
    limiter = _redis_limiter(BrokenRedis())

    with pytest.raises(LoginRateLimitUnavailable) as exc_info:
        limiter.check(source_ip="192.0.2.10", normalized_account="one@example.com")

    assert "synthetic Redis outage" not in str(exc_info.value)


def test_login_rate_limit_returns_uniform_429_retry_after_before_authentication() -> None:
    limiter = RejectingLimiter(LoginRateLimitExceeded(17))
    client, authentication = _api_client(limiter)

    response = client.post(
        "/api/v1/auth/sessions",
        json={"email": "  Analyst@Example.COM ", "password": "not-checked"},
        headers={"X-Forwarded-For": "203.0.113.99"},
        environ_overrides={"REMOTE_ADDR": "192.0.2.44"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert response.get_json()["error"] == {
        "code": "LOGIN_RATE_LIMITED",
        "message": "登录尝试过于频繁，请稍后重试",
    }
    assert limiter.calls == [
        {
            "source_ip": "192.0.2.44",
            "normalized_account": "analyst@example.com",
        }
    ]
    assert authentication.login_calls == 0


def test_enabled_limiter_failure_maps_to_stable_503_before_authentication() -> None:
    limiter = RejectingLimiter(LoginRateLimitUnavailable("private diagnostic"))
    client, authentication = _api_client(limiter)

    response = client.post(
        "/api/v1/auth/sessions",
        json={"email": "analyst@example.com", "password": "not-checked"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == {
        "code": "LOGIN_RATE_LIMIT_UNAVAILABLE",
        "message": "登录保护服务暂时不可用，请稍后重试",
    }
    assert "private diagnostic" not in response.get_data(as_text=True)
    assert authentication.login_calls == 0


def test_registration_uses_shared_limiter_before_password_hashing() -> None:
    limiter = RejectingLimiter(LoginRateLimitExceeded(23))
    registration = RegistrationMustNotRun()
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
    )
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(),
        authentication_service=AuthenticationMustNotRun(),
        invitation_registration_service=registration,
        login_rate_limiter=limiter,
    )
    app.config["TESTING"] = True

    response = app.test_client().post(
        "/api/v1/auth/registrations",
        json={
            "invitation_token": "unexamined-token",
            "email": " Invited@Example.com ",
            "display_name": "Invited Analyst",
            "password": "correct horse battery staple",
        },
        environ_overrides={"REMOTE_ADDR": "192.0.2.45"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "23"
    assert limiter.calls == [
        {
            "source_ip": "192.0.2.45",
            "normalized_account": "invited@example.com",
        }
    ]
    assert registration.registration_calls == 0


def test_create_app_does_not_construct_redis_client(monkeypatch) -> None:
    calls = []

    def forbidden_factory(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Redis construction must be request-lazy")

    monkeypatch.setattr(RedisLoginRateLimiter, "from_url", forbidden_factory)
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        rate_limit_enabled=True,
        redis_url="redis://127.0.0.1:6379/15",
        rate_limit_key_secret=SECRET,
    )

    create_app(settings=settings, analytics_service=MinimalAnalyticsService())

    assert calls == []
