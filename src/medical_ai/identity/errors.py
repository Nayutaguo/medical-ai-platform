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


class AdministrationError(RuntimeError):
    """Base class for stable, non-sensitive administration failures."""

    error_code = "administration_error"
    http_status = 400


class AdministrationValidationError(AdministrationError):
    """Validated administration input is malformed or outside hard limits."""

    error_code = "ADMIN_INVALID_REQUEST"
    http_status = 400

    def __init__(self, message: str = "管理请求参数不正确") -> None:
        super().__init__(message)


class AdministrationResourceNotFoundError(AdministrationError):
    """Do not reveal whether a resource exists in another organization."""

    error_code = "ADMIN_RESOURCE_NOT_FOUND"
    http_status = 404

    def __init__(self) -> None:
        super().__init__("管理对象不存在或不属于当前组织")


class AdministrationVersionConflictError(AdministrationError):
    """Raised when optimistic membership state is stale."""

    error_code = "ADMIN_VERSION_CONFLICT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("管理对象已被其他请求修改，请刷新后重试")


class AdministrationScopeConflictError(AdministrationError):
    """Raised for role/facility selections outside the active tenant."""

    error_code = "ADMIN_SCOPE_CONFLICT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("角色或机构范围与当前组织不匹配")


class AdministrationSelfLockoutError(AdministrationError):
    """Prevent an administrator from invalidating the final active control path."""

    error_code = "ADMIN_SELF_LOCKOUT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("不能停用当前成员关系或移除自身必需的管理权限")


class AdministrationLastManagerError(AdministrationError):
    """Prevent removal of the tenant's final active full manager."""

    error_code = "ADMIN_LAST_MANAGER"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("不能移除或停用当前组织最后一个有效管理成员")


class AdministrationStatusConflictError(AdministrationError):
    """Only active and suspended memberships participate in status toggles."""

    error_code = "ADMIN_STATUS_CONFLICT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("当前成员状态不允许停用或恢复")


class FacilityOwnershipConflictError(AdministrationError):
    """A canonical facility cannot be claimed by a second organization."""

    error_code = "FACILITY_OWNERSHIP_CONFLICT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("机构目录包含已归属其他组织的机构，本次同步未生效")


class FacilityCatalogScopeUnavailableError(AdministrationError):
    """The shared analytics dataset has no trusted binding to this tenant."""

    error_code = "FACILITY_CATALOG_SCOPE_UNAVAILABLE"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("当前组织未绑定住院数据集，不能同步机构目录")


class FacilityCatalogInvalidError(AdministrationError):
    """Source catalog rows violate the canonical facility contract."""

    error_code = "FACILITY_CATALOG_INVALID"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("分析数据中的机构目录不符合同步规则")
