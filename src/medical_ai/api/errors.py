from __future__ import annotations

from typing import Any

from flask import Flask
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from medical_ai.authorization import AuthorizationError
from medical_ai.identity.errors import (
    AdministrationError,
    AuthenticationRequiredError,
    CsrfValidationError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidInvitationError,
    InvalidPasswordResetError,
    InvitationCreationError,
    MembershipUnavailableError,
    OrganizationSelectionRequiredError,
)
from medical_ai.identity.passwords import PasswordPolicyError
from medical_ai.api.responses import current_request_id, error_response
from medical_ai.query import QueryValidationError
from medical_ai.rate_limit import LoginRateLimitExceeded, LoginRateLimitUnavailable
from medical_ai.services import ServiceUnavailableError, UpstreamServiceError, UpstreamTimeoutError


class APIError(Exception):
    """A safe, stable error that may be returned to an API client."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field_errors = field_errors


def register_error_handlers(app: Flask) -> None:
    """Register consistent API exception mapping."""

    @app.errorhandler(APIError)
    def handle_api_error(exc: APIError):
        app.logger.warning(
            "api_error request_id=%s code=%s status=%s",
            current_request_id(),
            exc.code,
            exc.status,
        )
        return error_response(
            status=exc.status,
            code=exc.code,
            message=exc.message,
            field_errors=exc.field_errors,
        )

    @app.errorhandler(LoginRateLimitExceeded)
    def handle_login_rate_limit(exc: LoginRateLimitExceeded):
        app.logger.warning(
            "login_rate_limited request_id=%s retry_after_seconds=%s",
            current_request_id(),
            exc.retry_after_seconds,
        )
        response, status = error_response(
            status=429,
            code="LOGIN_RATE_LIMITED",
            message="登录尝试过于频繁，请稍后重试",
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response, status

    @app.errorhandler(LoginRateLimitUnavailable)
    def handle_login_rate_limit_unavailable(exc: LoginRateLimitUnavailable):
        app.logger.error("login_rate_limit_unavailable request_id=%s", current_request_id())
        return error_response(
            status=503,
            code="LOGIN_RATE_LIMIT_UNAVAILABLE",
            message="登录保护服务暂时不可用，请稍后重试",
        )

    @app.errorhandler(QueryValidationError)
    def handle_query_validation_error(exc: QueryValidationError):
        app.logger.warning("query_validation_error request_id=%s", current_request_id())
        return error_response(
            status=400,
            code="INVALID_QUERY_SPEC",
            message=str(exc),
        )

    @app.errorhandler(InvalidCredentialsError)
    def handle_invalid_credentials(exc: InvalidCredentialsError):
        app.logger.warning("authentication_failed request_id=%s code=%s", current_request_id(), exc.error_code)
        return error_response(status=401, code="INVALID_CREDENTIALS", message=str(exc))

    @app.errorhandler(InvalidInvitationError)
    def handle_invalid_invitation(exc: InvalidInvitationError):
        app.logger.warning(
            "registration_failed request_id=%s code=%s",
            current_request_id(),
            exc.error_code,
        )
        return error_response(
            status=400,
            code="INVALID_INVITATION",
            message=str(exc),
        )

    @app.errorhandler(InvalidCurrentPasswordError)
    def handle_invalid_current_password(exc: InvalidCurrentPasswordError):
        app.logger.warning(
            "password_change_denied request_id=%s code=%s",
            current_request_id(),
            exc.error_code,
        )
        return error_response(
            status=400,
            code="CURRENT_PASSWORD_INVALID",
            message=str(exc),
        )

    @app.errorhandler(InvalidPasswordResetError)
    def handle_invalid_password_reset(exc: InvalidPasswordResetError):
        app.logger.warning(
            "password_reset_denied request_id=%s code=%s",
            current_request_id(),
            exc.error_code,
        )
        return error_response(
            status=400,
            code="PASSWORD_RESET_INVALID",
            message=str(exc),
        )

    @app.errorhandler(InvitationCreationError)
    def handle_invitation_creation(exc: InvitationCreationError):
        app.logger.warning(
            "invitation_creation_conflict request_id=%s code=%s",
            current_request_id(),
            exc.error_code,
        )
        return error_response(
            status=409,
            code="INVITATION_CONFLICT",
            message=str(exc),
        )

    @app.errorhandler(AdministrationError)
    def handle_administration_error(exc: AdministrationError):
        app.logger.warning(
            "administration_error request_id=%s code=%s status=%s",
            current_request_id(),
            exc.error_code,
            exc.http_status,
        )
        return error_response(
            status=exc.http_status,
            code=exc.error_code,
            message=str(exc),
        )

    @app.errorhandler(PasswordPolicyError)
    def handle_password_policy(exc: PasswordPolicyError):
        return error_response(
            status=400,
            code="PASSWORD_POLICY_VIOLATION",
            message="密码不符合安全要求",
            field_errors=[{"field": "password", "message": str(exc)}],
        )

    @app.errorhandler(AuthenticationRequiredError)
    def handle_authentication_required(exc: AuthenticationRequiredError):
        return error_response(status=401, code="AUTHENTICATION_REQUIRED", message=str(exc))

    @app.errorhandler(CsrfValidationError)
    def handle_csrf_failure(exc: CsrfValidationError):
        app.logger.warning("csrf_validation_failed request_id=%s", current_request_id())
        return error_response(status=403, code="CSRF_VALIDATION_FAILED", message=str(exc))

    @app.errorhandler(MembershipUnavailableError)
    def handle_membership_unavailable(exc: MembershipUnavailableError):
        return error_response(status=403, code="MEMBERSHIP_UNAVAILABLE", message=str(exc))

    @app.errorhandler(OrganizationSelectionRequiredError)
    def handle_organization_required(exc: OrganizationSelectionRequiredError):
        return error_response(status=409, code="ORGANIZATION_REQUIRED", message=str(exc))

    @app.errorhandler(AuthorizationError)
    def handle_authorization_error(exc: AuthorizationError):
        app.logger.warning(
            "authorization_denied request_id=%s code=%s",
            current_request_id(),
            exc.error_code,
        )
        return error_response(status=403, code="PERMISSION_DENIED", message="没有执行该操作的权限")

    @app.errorhandler(KeyError)
    def handle_allowlist_error(exc: KeyError):
        app.logger.warning("allowlist_error request_id=%s", current_request_id())
        return error_response(status=400, code="INVALID_FIELD", message=str(exc).strip("'"))

    @app.errorhandler(ServiceUnavailableError)
    def handle_service_unavailable(exc: ServiceUnavailableError):
        app.logger.warning("service_unavailable request_id=%s", current_request_id())
        return error_response(status=503, code="SERVICE_UNAVAILABLE", message=str(exc))

    @app.errorhandler(UpstreamServiceError)
    def handle_upstream_error(exc: UpstreamServiceError):
        app.logger.error("upstream_service_error request_id=%s", current_request_id(), exc_info=exc)
        return error_response(status=502, code="UPSTREAM_SERVICE_ERROR", message=str(exc))

    @app.errorhandler(UpstreamTimeoutError)
    def handle_upstream_timeout(exc: UpstreamTimeoutError):
        app.logger.error("upstream_timeout request_id=%s", current_request_id(), exc_info=exc)
        return error_response(status=504, code="UPSTREAM_TIMEOUT", message=str(exc))

    @app.errorhandler(SQLAlchemyError)
    def handle_database_error(exc: SQLAlchemyError):
        # Database exceptions can embed hosts, schema names, SQL, or driver
        # details.  Record only the stable category and correlation ID here;
        # dependency diagnostics belong in restricted infrastructure logs.
        app.logger.error(
            "database_error request_id=%s error_type=%s",
            current_request_id(),
            type(exc).__name__,
        )
        return error_response(
            status=503,
            code="DATABASE_UNAVAILABLE",
            message="数据服务暂时不可用，请稍后重试",
        )

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        status = int(exc.code or 500)
        codes: dict[int, tuple[str, str]] = {
            400: ("BAD_REQUEST", "请求格式不正确"),
            404: ("NOT_FOUND", "请求的资源不存在"),
            405: ("METHOD_NOT_ALLOWED", "请求方法不受支持"),
            413: ("PAYLOAD_TOO_LARGE", "请求内容超过大小限制"),
            415: ("UNSUPPORTED_MEDIA_TYPE", "请求必须使用 application/json"),
        }
        code, message = codes.get(status, ("HTTP_ERROR", "请求处理失败"))
        return error_response(status=status, code=code, message=message)

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        app.logger.error("unhandled_error request_id=%s", current_request_id(), exc_info=exc)
        return error_response(
            status=500,
            code="INTERNAL_ERROR",
            message="服务内部错误，请使用 request_id 联系管理员",
        )
