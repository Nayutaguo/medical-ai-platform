"""Security-posture tests for local WSGI entry points."""

from medical_ai.api.auth_dev_wsgi import app as authenticated_development_app


def test_authenticated_development_wsgi_enables_governed_browser_path() -> None:
    settings = authenticated_development_app.extensions["settings"]

    assert settings.app_environment == "development"
    assert settings.auth_enforcement_enabled is True
    assert settings.auth_session_cookie_secure is False
    assert settings.rate_limit_enabled is False
    assert settings.mcp_allow_unscoped_tools is False
