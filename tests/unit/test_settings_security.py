import pytest
from pydantic import ValidationError

from medical_ai.config import Settings


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "auth_enforcement_enabled": False,
            "auth_session_cookie_secure": True,
            "mcp_allow_unscoped_tools": False,
        },
        {
            "auth_enforcement_enabled": True,
            "auth_session_cookie_secure": False,
            "mcp_allow_unscoped_tools": False,
        },
        {
            "auth_enforcement_enabled": True,
            "auth_session_cookie_secure": True,
            "mcp_allow_unscoped_tools": True,
        },
    ],
)
def test_production_rejects_insecure_governance_configuration(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_environment="production",
            rate_limit_enabled=True,
            redis_url="rediss://redis.internal:6379/0",
            rate_limit_key_secret="production-test-secret-with-32-bytes",
            **overrides,
        )


def test_production_accepts_fail_closed_governance_configuration() -> None:
    settings = Settings(
        _env_file=None,
        app_environment="production",
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=True,
        mcp_allow_unscoped_tools=False,
        rate_limit_enabled=True,
        redis_url="rediss://redis.internal:6379/0",
        rate_limit_key_secret="production-test-secret-with-32-bytes",
    )

    assert settings.auth_enforcement_enabled is True
    assert settings.mcp_allow_unscoped_tools is False
    assert settings.rate_limit_enabled is True


@pytest.mark.parametrize(
    "rate_limit_overrides",
    [
        {"rate_limit_enabled": False},
        {
            "rate_limit_enabled": True,
            "redis_url": "",
            "rate_limit_key_secret": "x" * 32,
        },
        {
            "rate_limit_enabled": True,
            "redis_url": "redis://127.0.0.1:6379/0",
            "rate_limit_key_secret": "too-short",
        },
    ],
)
def test_production_rejects_missing_login_rate_limit_protection(rate_limit_overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_environment="production",
            auth_enforcement_enabled=True,
            auth_session_cookie_secure=True,
            mcp_allow_unscoped_tools=False,
            **rate_limit_overrides,
        )


def test_rate_limit_secrets_are_excluded_from_settings_repr() -> None:
    settings = Settings(
        _env_file=None,
        rate_limit_enabled=True,
        redis_url="redis://username:private-password@redis.internal:6379/0",
        rate_limit_key_secret="private-rate-limit-key-secret-32-bytes",
    )

    rendered = repr(settings)
    assert "private-password" not in rendered
    assert "private-rate-limit-key" not in rendered
