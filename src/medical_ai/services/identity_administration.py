"""One-time identity bootstrap and future administration orchestration."""

from __future__ import annotations

import re
from uuid import uuid4

from medical_ai.authorization import PermissionCode
from medical_ai.identity.emails import normalize_ascii_email
from medical_ai.identity.models import (
    BootstrapPlan,
    BootstrapResult,
    BootstrapRole,
)
from medical_ai.identity.passwords import Argon2idPasswordService
from medical_ai.identity.ports import IdentityBootstrapRepositoryPort


SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class IdentityAdministrationService:
    """Create the first organization and least-privilege administrator."""

    def __init__(
        self,
        repository: IdentityBootstrapRepositoryPort,
        *,
        password_service: Argon2idPasswordService | None = None,
    ) -> None:
        self.repository = repository
        self.password_service = password_service or Argon2idPasswordService()

    def bootstrap_first_administrator(
        self,
        *,
        email: str,
        display_name: str,
        password: str,
        organization_name: str,
        organization_slug: str,
    ) -> BootstrapResult:
        """Build and atomically persist a first-user bootstrap plan.

        The assigned organization-administrator role intentionally excludes all
        analytical data permissions. A separate data-analyst role is seeded but
        must be assigned explicitly after facility scopes are configured.
        """

        normalized_email = _validated_email(email)
        normalized_display_name = _bounded_text(display_name, "display_name", 120)
        normalized_organization_name = _bounded_text(
            organization_name,
            "organization_name",
            200,
        )
        normalized_slug = organization_slug.strip().casefold()
        if not SLUG_PATTERN.fullmatch(normalized_slug):
            raise ValueError("organization_slug 只能包含小写字母、数字和单个连字符")

        permission_ids = tuple((permission, str(uuid4())) for permission in PermissionCode)
        administration_permissions = frozenset(
            {
                PermissionCode.USERS_MANAGE,
                PermissionCode.ROLES_ASSIGN,
                PermissionCode.AUDIT_READ,
                PermissionCode.IMPORTS_CREATE,
            }
        )
        analyst_permissions = frozenset(
            {
                PermissionCode.ANALYTICS_QUERY_EXECUTE,
                PermissionCode.ANALYTICS_AGENT_EXECUTE,
                PermissionCode.ANALYTICS_SCHEMA_READ,
                PermissionCode.ANALYTICS_DISTINCT_READ,
            }
        )
        plan = BootstrapPlan(
            user_id=str(uuid4()),
            email=email.strip(),
            email_normalized=normalized_email,
            display_name=normalized_display_name,
            password_hash=self.password_service.hash_password(password),
            organization_id=str(uuid4()),
            organization_name=normalized_organization_name,
            organization_slug=normalized_slug,
            membership_id=str(uuid4()),
            permission_ids=permission_ids,
            roles=(
                BootstrapRole(
                    role_id=str(uuid4()),
                    role_key="organization_admin",
                    name="Organization Administrator",
                    permissions=administration_permissions,
                    assign_to_bootstrap_membership=True,
                ),
                BootstrapRole(
                    role_id=str(uuid4()),
                    role_key="data_analyst",
                    name="Data Analyst",
                    permissions=analyst_permissions,
                ),
            ),
        )
        return self.repository.bootstrap_first_administrator(plan)


def _validated_email(value: str) -> str:
    normalized = normalize_ascii_email(value)
    if normalized is None:
        raise ValueError("email 格式不正确")
    return normalized


def _bounded_text(value: str, field: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{field} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field} 超过 {maximum} 个字符")
    return normalized
