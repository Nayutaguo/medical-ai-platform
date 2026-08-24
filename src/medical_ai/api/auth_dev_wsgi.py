"""Loopback-only WSGI entry point for testing browser authentication locally.

This development posture enables the governed HTTP authorization path while
keeping cookies usable over plain ``http://127.0.0.1``. It deliberately does
not weaken the production entry point and must not be exposed beyond loopback.
"""

from medical_ai.api import create_app
from medical_ai.config import Settings


app = create_app(
    Settings(
        app_environment="development",
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        rate_limit_enabled=False,
        mcp_allow_unscoped_tools=False,
    )
)
