from __future__ import annotations

import re
import time
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, g, request, send_from_directory

from medical_ai.api.admin_routes import admin_api
from medical_ai.api.auth_routes import auth_api
from medical_ai.api.errors import register_error_handlers
from medical_ai.api.history_routes import history_api
from medical_ai.api.routes import api_v1
from medical_ai.config import Settings, get_settings
from medical_ai.services import AnalyticsService

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def create_app(
    settings: Settings | None = None,
    analytics_service: AnalyticsService | None = None,
    authentication_service=None,
    invitation_registration_service=None,
    login_rate_limiter=None,
    governance_administration_service=None,
    history_service=None,
) -> Flask:
    """Create the Flask application without connecting to external services eagerly."""

    settings = settings or get_settings()
    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    app.config.update(
        MAX_CONTENT_LENGTH=settings.api_max_body_bytes,
        JSON_SORT_KEYS=False,
    )
    app.extensions["settings"] = settings
    app.extensions["analytics_service"] = analytics_service or AnalyticsService(settings=settings)
    app.extensions["authentication_service"] = authentication_service
    app.extensions["invitation_registration_service"] = invitation_registration_service
    app.extensions["login_rate_limiter"] = login_rate_limiter
    app.extensions["governance_administration_service"] = governance_administration_service
    app.extensions["history_service"] = history_service
    app.register_blueprint(api_v1)
    app.register_blueprint(history_api)
    app.register_blueprint(auth_api)
    app.register_blueprint(admin_api)

    @app.before_request
    def inject_request_context() -> None:
        candidate = request.headers.get("X-Request-Id", "")
        g.request_id = candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid4())
        g.request_started = time.perf_counter()

    @app.after_request
    def finalize_response(response):
        duration_ms = round((time.perf_counter() - g.request_started) * 1000, 3)
        response.headers["X-Request-Id"] = g.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/v1/"):
            # Medical aggregates, authorization state, and errors must not be
            # retained by browsers or intermediary HTTP caches. Application
            # caching belongs behind the authorization/privacy boundary.
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        app.logger.info(
            "request_completed request_id=%s method=%s path=%s status=%s duration_ms=%s",
            g.request_id,
            request.method,
            request.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.get("/")
    def frontend_index():
        if not (frontend_dist / "index.html").is_file():
            abort(404)
        return send_from_directory(frontend_dist, "index.html")

    @app.get("/<path:asset_path>")
    def frontend_asset(asset_path: str):
        if asset_path.startswith("api/"):
            abort(404)
        candidate = (frontend_dist / asset_path).resolve()
        if frontend_dist.resolve() in candidate.parents and candidate.is_file():
            return send_from_directory(frontend_dist, asset_path)
        if (frontend_dist / "index.html").is_file():
            return send_from_directory(frontend_dist, "index.html")
        abort(404)

    register_error_handlers(app)
    return app
