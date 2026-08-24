"""Stable authentication-domain errors safe for API mapping."""

from __future__ import annotations


class AuthenticationError(RuntimeError):
    """Base class for expected authentication failures."""

    error_code = "authentication_error"


class InvalidCredentialsError(AuthenticationError):
    """Use one response for unknown, disabled, and mismatched credentials."""

    error_code = "invalid_credentials"

    def __init__(self) -> None:
        super().__init__("邮箱或密码不正确")


class OrganizationSelectionRequiredError(AuthenticationError):
    """Raised after valid authentication when an organization is ambiguous."""

    error_code = "organization_required"

    def __init__(self) -> None:
        super().__init__("该账号属于多个组织，请指定 organization_id")


class MembershipUnavailableError(AuthenticationError):
    """Raised when no active membership can be selected."""

    error_code = "membership_unavailable"

    def __init__(self) -> None:
        super().__init__("当前账号没有可用的组织成员关系")


class AuthenticationRequiredError(AuthenticationError):
    """Raised for absent, expired, revoked, or stale sessions."""

    error_code = "authentication_required"

    def __init__(self) -> None:
        super().__init__("请先登录或重新登录")


class CsrfValidationError(AuthenticationError):
    """Raised when a state-changing browser request lacks CSRF proof."""

    error_code = "csrf_validation_failed"

    def __init__(self) -> None:
        super().__init__("请求安全校验失败，请刷新页面后重试")


class BootstrapAlreadyCompletedError(AuthenticationError):
    """Raised when the one-time administrator bootstrap was already used."""

    error_code = "bootstrap_already_completed"

    def __init__(self) -> None:
        super().__init__("控制面已经存在用户，禁止重复执行初始化管理员流程")


class InvitationError(AuthenticationError):
    """Base class for invitation issuance and registration failures."""

    error_code = "invitation_error"


class InvitationCreationError(InvitationError):
    """Use one response for duplicate users, tenants, and write races."""

    error_code = "invitation_creation_failed"

    def __init__(self) -> None:
        super().__init__("无法创建邀请，请核对组织和账号状态")


class InvalidInvitationError(InvitationError):
    """Indistinguishable response for wrong, expired, or consumed tokens."""

    error_code = "invitation_unavailable"

    def __init__(self) -> None:
        super().__init__("邀请无效或已过期，请联系管理员重新邀请")
