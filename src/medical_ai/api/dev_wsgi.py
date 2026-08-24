"""Loopback-only WSGI entry point for the transitional local workbench."""

from medical_ai.api import create_app
from medical_ai.config import Settings

app = create_app(
    Settings(
        app_environment="development",
        auth_enforcement_enabled=False,
        auth_session_cookie_secure=False,
        mcp_allow_unscoped_tools=True,
    )
)
