#!/usr/bin/env python
"""Run the versioned Flask API and the built frontend for local development."""

import argparse
import logging

from medical_ai.api import create_app
from medical_ai.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the medical AI platform Flask server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("the anonymous development runner is restricted to loopback hosts")

    logging.basicConfig(level=logging.INFO)
    app = create_app(
        Settings(
            app_environment="development",
            auth_enforcement_enabled=False,
            auth_session_cookie_secure=False,
            mcp_allow_unscoped_tools=True,
        )
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
