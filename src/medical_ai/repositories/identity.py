"""SQLAlchemy repository for identity credentials and durable sessions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from hmac import compare_digest

from sqlalchemy import and_, create_engine, or_, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.config import Settings, get_settings
from medical_ai.identity import (
    audit_events,
    auth_sessions,
    facilities,
    membership_facility_scopes,
    membership_roles,
    one_time_tokens,
    organization_facilities,
    organization_memberships,
    organizations,
    permissions,
    role_permissions,
    roles,
    users,
)
from medical_ai.identity.errors import (
    BootstrapAlreadyCompletedError,
    InvalidInvitationError,
    InvitationCreationError,
)
from medical_ai.identity.models import (
    ActiveSession,
    BootstrapPlan,
    BootstrapResult,
    InvitationRegistrationPlan,
    MembershipGrant,
    NewSession,
    PersistedUserInvitation,
    RegistrationResult,
    USER_INVITATION_PURPOSE,
    UserInvitationPlan,
    UserCredential,
)


class IdentityRepository:
    """Persist identity state using the current control-plane MySQL database.

    The first rollout may share the existing MySQL instance.  A separately
    privileged control-plane URL can replace the injected engine without
    changing the authentication service.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or create_engine(
            self.settings.mysql_url(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10},
        )

    def find_user_by_normalized_email(self, normalized_email: str) -> UserCredential | None:
        """Load one credential record by its unique normalized identifier."""

        statement = select(
            users.c.id,
            users.c.email,
            users.c.display_name,
            users.c.status,
            users.c.password_hash,
            users.c.password_algorithm,
            users.c.auth_version,
        ).where(users.c.email_normalized == normalized_email)
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return _user_from_row(row) if row else None

    def bootstrap_first_administrator(self, plan: BootstrapPlan) -> BootstrapResult:
        """Persist the first user, tenant, built-in roles, and grants atomically."""

        permission_ids = dict(plan.permission_ids)
        with self.engine.begin() as connection:
            # The deployment bootstrap is a single-instance operation. The
            # locking read prevents a concurrent first-user insert in InnoDB;
            # all natural identifiers remain protected by UNIQUE constraints.
            existing_user_id = connection.execute(
                select(users.c.id).order_by(users.c.id).limit(1).with_for_update()
            ).scalar_one_or_none()
            if existing_user_id is not None:
                raise BootstrapAlreadyCompletedError

            connection.execute(
                organizations.insert().values(
                    id=plan.organization_id,
                    name=plan.organization_name,
                    slug=plan.organization_slug,
                    status="active",
                )
            )
            connection.execute(
                users.insert().values(
                    id=plan.user_id,
                    email=plan.email,
                    email_normalized=plan.email_normalized,
                    password_hash=plan.password_hash,
                    password_algorithm="argon2id",
                    display_name=plan.display_name,
                    status="active",
                    email_verified_at=text("UTC_TIMESTAMP(6)"),
                )
            )
            connection.execute(
                organization_memberships.insert().values(
                    id=plan.membership_id,
                    organization_id=plan.organization_id,
                    user_id=plan.user_id,
                    status="active",
                )
            )
            connection.execute(
                permissions.insert(),
                [
                    {
                        "id": permission_id,
                        "permission_key": permission.value,
                        "resource": permission.value.rsplit(".", 1)[0],
                        "action": permission.value.rsplit(".", 1)[1],
                        "description": f"Built-in permission {permission.value}",
                    }
                    for permission, permission_id in plan.permission_ids
                ],
            )
            for role_definition in plan.roles:
                connection.execute(
                    roles.insert().values(
                        id=role_definition.role_id,
                        organization_id=plan.organization_id,
                        role_key=role_definition.role_key,
                        name=role_definition.name,
                        is_system=True,
                    )
                )
                connection.execute(
                    role_permissions.insert(),
                    [
                        {
                            "role_id": role_definition.role_id,
                            "permission_id": permission_ids[permission],
                            "granted_by_user_id": plan.user_id,
                        }
                        for permission in sorted(
                            role_definition.permissions,
                            key=lambda item: item.value,
                        )
                    ],
                )
                if role_definition.assign_to_bootstrap_membership:
                    connection.execute(
                        membership_roles.insert().values(
                            organization_id=plan.organization_id,
                            membership_id=plan.membership_id,
                            role_id=role_definition.role_id,
                            assigned_by_user_id=plan.user_id,
                        )
                    )

            connection.execute(
                audit_events.insert().values(
                    organization_id=plan.organization_id,
                    actor_kind="user",
                    actor_membership_id=plan.membership_id,
                    actor_user_id=plan.user_id,
                    action="identity.bootstrap",
                    resource_type="organization",
                    resource_id=plan.organization_id,
                    outcome="success",
                    details={
                        "roles_created": [role.role_key for role in plan.roles],
                        "assigned_role": "organization_admin",
                    },
                )
            )

        return BootstrapResult(
            user_id=plan.user_id,
            organization_id=plan.organization_id,
            membership_id=plan.membership_id,
        )

    def create_user_invitation(
        self,
        plan: UserInvitationPlan,
    ) -> PersistedUserInvitation:
        """Create or safely reissue an invited identity in one transaction."""

        try:
            with self.engine.begin() as connection:
                organization_exists = connection.execute(
                    select(organizations.c.id)
                    .where(
                        organizations.c.id == plan.organization_id,
                        organizations.c.status == "active",
                        organizations.c.deleted_at.is_(None),
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if organization_exists is None:
                    raise InvitationCreationError

                existing_user = connection.execute(
                    select(
                        users.c.id,
                        users.c.status,
                        users.c.password_hash,
                        users.c.password_algorithm,
                        users.c.auth_version,
                        users.c.deleted_at,
                    )
                    .where(users.c.email_normalized == plan.email_normalized)
                    .with_for_update()
                ).mappings().one_or_none()

                if existing_user is None:
                    user_id = plan.user_id
                    membership_id = plan.membership_id
                    identity_version = plan.identity_version
                    connection.execute(
                        users.insert().values(
                            id=user_id,
                            email=plan.email,
                            email_normalized=plan.email_normalized,
                            password_hash=None,
                            password_algorithm=None,
                            display_name=plan.placeholder_display_name,
                            status="invited",
                            auth_version=identity_version,
                            created_at=plan.created_at,
                            updated_at=plan.created_at,
                        )
                    )
                    connection.execute(
                        organization_memberships.insert().values(
                            id=membership_id,
                            organization_id=plan.organization_id,
                            user_id=user_id,
                            status="invited",
                            joined_at=plan.created_at,
                            created_at=plan.created_at,
                            updated_at=plan.created_at,
                        )
                    )
                else:
                    if not _user_can_be_reinvited(existing_user):
                        raise InvitationCreationError
                    user_id = str(existing_user["id"])
                    identity_version = int(existing_user["auth_version"])
                    existing_membership = connection.execute(
                        select(
                            organization_memberships.c.id,
                            organization_memberships.c.status,
                        )
                        .where(
                            organization_memberships.c.organization_id
                            == plan.organization_id,
                            organization_memberships.c.user_id == user_id,
                        )
                        .with_for_update()
                    ).mappings().one_or_none()
                    if (
                        existing_membership is None
                        or existing_membership["status"] != "invited"
                    ):
                        raise InvitationCreationError
                    membership_id = str(existing_membership["id"])
                    connection.execute(
                        update(one_time_tokens)
                        .where(
                            one_time_tokens.c.organization_id
                            == plan.organization_id,
                            one_time_tokens.c.membership_id == membership_id,
                            one_time_tokens.c.user_id == user_id,
                            one_time_tokens.c.purpose == USER_INVITATION_PURPOSE,
                            one_time_tokens.c.consumed_at.is_(None),
                        )
                        .values(
                            consumed_at=plan.created_at,
                            version=one_time_tokens.c.version + 1,
                        )
                    )

                connection.execute(
                    one_time_tokens.insert().values(
                        id=plan.token_id,
                        organization_id=plan.organization_id,
                        membership_id=membership_id,
                        user_id=user_id,
                        purpose=USER_INVITATION_PURPOSE,
                        token_hash=plan.token_hash,
                        identity_version=identity_version,
                        expires_at=plan.expires_at,
                        created_at=plan.created_at,
                    )
                )
                connection.execute(
                    audit_events.insert().values(
                        occurred_at=plan.created_at,
                        request_id=plan.request_id,
                        organization_id=plan.organization_id,
                        actor_kind=plan.actor_kind,
                        actor_membership_id=plan.actor_membership_id,
                        actor_user_id=plan.actor_user_id,
                        action="identity.invitation.create",
                        resource_type="membership",
                        resource_id=membership_id,
                        outcome="success",
                        details={},
                    )
                )
                return PersistedUserInvitation(
                    user_id=user_id,
                    organization_id=plan.organization_id,
                    membership_id=membership_id,
                )
        except IntegrityError:
            # Duplicate emails and actor/context races are deliberately
            # indistinguishable at this persistence boundary.
            raise InvitationCreationError from None

    def consume_user_invitation(
        self,
        plan: InvitationRegistrationPlan,
    ) -> RegistrationResult:
        """Consume one exact invitation and activate its identity atomically."""

        invitation_context = (
            one_time_tokens.join(users, users.c.id == one_time_tokens.c.user_id)
            .join(
                organization_memberships,
                and_(
                    organization_memberships.c.id == one_time_tokens.c.membership_id,
                    organization_memberships.c.user_id == one_time_tokens.c.user_id,
                    organization_memberships.c.organization_id
                    == one_time_tokens.c.organization_id,
                ),
            )
            .join(
                organizations,
                organizations.c.id == one_time_tokens.c.organization_id,
            )
        )
        statement = (
            select(
                one_time_tokens.c.id.label("token_id"),
                one_time_tokens.c.organization_id,
                one_time_tokens.c.membership_id,
                one_time_tokens.c.user_id,
                one_time_tokens.c.identity_version.label("token_identity_version"),
                one_time_tokens.c.expires_at,
                one_time_tokens.c.consumed_at,
                one_time_tokens.c.version.label("token_version"),
                users.c.email,
                users.c.email_normalized,
                users.c.password_hash,
                users.c.status.label("user_status"),
                users.c.auth_version,
                users.c.version.label("user_version"),
                organization_memberships.c.status.label("membership_status"),
                organization_memberships.c.version.label("membership_version"),
                organizations.c.status.label("organization_status"),
                organizations.c.deleted_at.label("organization_deleted_at"),
            )
            .select_from(invitation_context)
            .where(
                one_time_tokens.c.token_hash == plan.token_hash,
                one_time_tokens.c.purpose == USER_INVITATION_PURPOSE,
            )
            .with_for_update()
        )

        try:
            with self.engine.begin() as connection:
                row = connection.execute(statement).mappings().one_or_none()
                if not _registration_context_is_valid(row, plan):
                    raise InvalidInvitationError

                token_update = connection.execute(
                    update(one_time_tokens)
                    .where(
                        one_time_tokens.c.id == row["token_id"],
                        one_time_tokens.c.consumed_at.is_(None),
                        one_time_tokens.c.expires_at > plan.completed_at,
                        one_time_tokens.c.identity_version == row["auth_version"],
                        one_time_tokens.c.version == row["token_version"],
                    )
                    .values(
                        consumed_at=plan.completed_at,
                        version=one_time_tokens.c.version + 1,
                    )
                )
                user_update = connection.execute(
                    update(users)
                    .where(
                        users.c.id == row["user_id"],
                        users.c.email_normalized == plan.email_normalized,
                        users.c.status == "invited",
                        users.c.password_hash.is_(None),
                        users.c.auth_version == row["token_identity_version"],
                        users.c.version == row["user_version"],
                    )
                    .values(
                        display_name=plan.display_name,
                        password_hash=plan.password_hash,
                        password_algorithm=plan.password_algorithm,
                        status="active",
                        email_verified_at=plan.completed_at,
                        auth_version=users.c.auth_version + 1,
                        updated_at=plan.completed_at,
                        version=users.c.version + 1,
                    )
                )
                membership_update = connection.execute(
                    update(organization_memberships)
                    .where(
                        organization_memberships.c.id == row["membership_id"],
                        organization_memberships.c.user_id == row["user_id"],
                        organization_memberships.c.organization_id
                        == row["organization_id"],
                        organization_memberships.c.status == "invited",
                        organization_memberships.c.version
                        == row["membership_version"],
                    )
                    .values(
                        status="active",
                        joined_at=plan.completed_at,
                        updated_at=plan.completed_at,
                        version=organization_memberships.c.version + 1,
                    )
                )
                if (
                    token_update.rowcount != 1
                    or user_update.rowcount != 1
                    or membership_update.rowcount != 1
                ):
                    raise InvalidInvitationError

                connection.execute(
                    audit_events.insert().values(
                        occurred_at=plan.completed_at,
                        request_id=plan.request_id,
                        organization_id=row["organization_id"],
                        actor_kind="system",
                        actor_membership_id=None,
                        actor_user_id=None,
                        action="identity.registration.complete",
                        resource_type="membership",
                        resource_id=row["membership_id"],
                        outcome="success",
                        details={},
                    )
                )
                return RegistrationResult(
                    user_id=str(row["user_id"]),
                    organization_id=str(row["organization_id"]),
                    membership_id=str(row["membership_id"]),
                    email=str(row["email"]),
                    display_name=plan.display_name,
                )
        except IntegrityError:
            raise InvalidInvitationError from None

    def list_active_memberships(self, user_id: str) -> list[MembershipGrant]:
        """Resolve active organizations and explicit permission grants."""

        membership_join = (
            organization_memberships.join(
                organizations,
                organizations.c.id == organization_memberships.c.organization_id,
            )
            .outerjoin(
                membership_roles,
                membership_roles.c.membership_id == organization_memberships.c.id,
            )
            .outerjoin(
                role_permissions,
                role_permissions.c.role_id == membership_roles.c.role_id,
            )
            .outerjoin(
                permissions,
                permissions.c.id == role_permissions.c.permission_id,
            )
        )
        statement = (
            select(
                organization_memberships.c.id.label("membership_id"),
                organization_memberships.c.organization_id,
                organization_memberships.c.status,
                organization_memberships.c.authorization_version,
                organizations.c.name.label("organization_name"),
                permissions.c.permission_key,
            )
            .select_from(membership_join)
            .where(
                organization_memberships.c.user_id == user_id,
                organization_memberships.c.status == "active",
                organizations.c.status == "active",
            )
            .order_by(organization_memberships.c.id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            membership_contexts = {
                str(row["membership_id"]): str(row["organization_id"])
                for row in rows
            }
            facility_values = _facility_values_by_membership(
                connection,
                membership_contexts,
            )
        return _group_membership_rows(rows, facility_values)

    def create_session(self, session: NewSession, *, logged_in_at: datetime) -> None:
        """Insert a hash-only session and update successful-login time atomically."""

        with self.engine.begin() as connection:
            connection.execute(
                auth_sessions.insert().values(
                    id=session.session_id,
                    membership_id=session.membership_id,
                    user_id=session.user_id,
                    organization_id=session.organization_id,
                    session_token_hash=session.session_token_hash,
                    csrf_token_hash=session.csrf_token_hash,
                    expires_at=session.expires_at,
                    idle_expires_at=session.idle_expires_at,
                    identity_version=session.identity_version,
                    authorization_version=session.authorization_version,
                )
            )
            connection.execute(
                update(users)
                .where(users.c.id == session.user_id)
                .values(
                    last_login_at=logged_in_at,
                    updated_at=logged_in_at,
                    version=users.c.version + 1,
                )
            )

    def find_active_session(self, token_hash: bytes, *, now: datetime) -> ActiveSession | None:
        """Resolve a session only while all status and version checks remain valid."""

        session_join = (
            auth_sessions.join(users, users.c.id == auth_sessions.c.user_id)
            .join(
                organization_memberships,
                and_(
                    organization_memberships.c.id == auth_sessions.c.membership_id,
                    organization_memberships.c.user_id == auth_sessions.c.user_id,
                    organization_memberships.c.organization_id == auth_sessions.c.organization_id,
                ),
            )
            .join(organizations, organizations.c.id == auth_sessions.c.organization_id)
        )
        statement = (
            select(
                auth_sessions.c.id.label("session_id"),
                auth_sessions.c.user_id,
                auth_sessions.c.membership_id,
                auth_sessions.c.organization_id,
                auth_sessions.c.expires_at,
                auth_sessions.c.idle_expires_at,
                auth_sessions.c.csrf_token_hash,
                users.c.email,
                users.c.display_name,
                users.c.auth_version,
                organization_memberships.c.authorization_version,
                organizations.c.name.label("organization_name"),
            )
            .select_from(session_join)
            .where(
                auth_sessions.c.session_token_hash == token_hash,
                auth_sessions.c.revoked_at.is_(None),
                auth_sessions.c.expires_at > now,
                auth_sessions.c.idle_expires_at > now,
                users.c.status == "active",
                organizations.c.status == "active",
                organization_memberships.c.status == "active",
                auth_sessions.c.identity_version == users.c.auth_version,
                auth_sessions.c.authorization_version
                == organization_memberships.c.authorization_version,
            )
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            membership_id = str(row["membership_id"])
            organization_id = str(row["organization_id"])
            permission_values = _permission_values(connection, membership_id)
            facility_values = _facility_values_by_membership(
                connection,
                {membership_id: organization_id},
            ).get(membership_id, frozenset())

        context = AccessContext(
            user_id=str(row["user_id"]),
            organization_id=organization_id,
            membership_id=membership_id,
            permissions=permission_values,
            allowed_facility_ids=facility_values,
            identity_version=int(row["auth_version"]),
            authorization_version=int(row["authorization_version"]),
        )
        return ActiveSession(
            session_id=str(row["session_id"]),
            user_id=str(row["user_id"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            organization_name=str(row["organization_name"]),
            expires_at=row["expires_at"],
            idle_expires_at=row["idle_expires_at"],
            csrf_token_hash=bytes(row["csrf_token_hash"]),
            access_context=context,
        )

    def touch_session(
        self,
        session_id: str,
        *,
        seen_at: datetime,
        idle_expires_at: datetime,
    ) -> None:
        """Extend idle expiry monotonically without changing absolute expiry.

        Concurrent requests can finish out of order.  The conditional update
        prevents an older request from moving ``last_seen_at`` or the idle
        deadline backwards after a newer request has already extended it.
        """

        statement = (
            update(auth_sessions)
            .where(
                auth_sessions.c.id == session_id,
                auth_sessions.c.revoked_at.is_(None),
                auth_sessions.c.idle_expires_at < idle_expires_at,
                auth_sessions.c.expires_at >= idle_expires_at,
            )
            .values(
                last_seen_at=seen_at,
                idle_expires_at=idle_expires_at,
                updated_at=seen_at,
                version=auth_sessions.c.version + 1,
            )
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def revoke_session(self, session_id: str, *, revoked_at: datetime) -> None:
        """Idempotently revoke one session."""

        statement = (
            update(auth_sessions)
            .where(auth_sessions.c.id == session_id, auth_sessions.c.revoked_at.is_(None))
            .values(
                revoked_at=revoked_at,
                updated_at=revoked_at,
                version=auth_sessions.c.version + 1,
            )
        )
        with self.engine.begin() as connection:
            connection.execute(statement)


def _registration_context_is_valid(
    row: Mapping[str, object] | None,
    plan: InvitationRegistrationPlan,
) -> bool:
    """Validate every invitation binding without exposing which fact failed."""

    if row is None:
        return False
    email_normalized = row["email_normalized"]
    expires_at = row["expires_at"]
    return bool(
        isinstance(email_normalized, str)
        and compare_digest(email_normalized, plan.email_normalized)
        and row["consumed_at"] is None
        and isinstance(expires_at, datetime)
        and expires_at > plan.completed_at
        and row["user_status"] == "invited"
        and row["membership_status"] == "invited"
        and row["organization_status"] == "active"
        and row["organization_deleted_at"] is None
        and row["password_hash"] is None
        and int(row["token_identity_version"]) == int(row["auth_version"])
    )


def _user_can_be_reinvited(row: Mapping[str, object]) -> bool:
    """Allow reissue only before an identity has ever been activated."""

    return bool(
        row["status"] == "invited"
        and row["password_hash"] is None
        and row["password_algorithm"] is None
        and row["deleted_at"] is None
        and int(row["auth_version"]) >= 1
    )


def _permission_values(connection: Connection, membership_id: str) -> frozenset[PermissionCode]:
    statement = (
        select(permissions.c.permission_key)
        .select_from(
            membership_roles.join(
                role_permissions,
                role_permissions.c.role_id == membership_roles.c.role_id,
            ).join(permissions, permissions.c.id == role_permissions.c.permission_id)
        )
        .where(membership_roles.c.membership_id == membership_id)
    )
    values: set[PermissionCode] = set()
    for permission_key in connection.execute(statement).scalars():
        try:
            values.add(PermissionCode(permission_key))
        except ValueError:
            # Unknown grants never widen access on an older application build.
            continue
    return frozenset(values)


def _facility_values_by_membership(
    connection: Connection,
    membership_contexts: Mapping[str, str],
) -> dict[str, frozenset[str]]:
    """Resolve active facility keys for exact membership/organization pairs.

    This query is deliberately separate from permission resolution so multiple
    permissions and multiple facilities cannot multiply one another. Missing
    scope rows return no entry and are interpreted by callers as an empty,
    fail-closed scope.
    """

    if not membership_contexts:
        return {}

    scope_join = membership_facility_scopes.join(
        organization_facilities,
        and_(
            organization_facilities.c.organization_id
            == membership_facility_scopes.c.organization_id,
            organization_facilities.c.facility_id
            == membership_facility_scopes.c.facility_id,
        ),
    ).join(
        facilities,
        facilities.c.id == organization_facilities.c.facility_id,
    )
    exact_context_predicates = [
        and_(
            membership_facility_scopes.c.membership_id == membership_id,
            membership_facility_scopes.c.organization_id == organization_id,
        )
        for membership_id, organization_id in membership_contexts.items()
    ]
    statement = (
        select(
            membership_facility_scopes.c.membership_id,
            facilities.c.facility_key,
        )
        .select_from(scope_join)
        .where(
            or_(*exact_context_predicates),
            facilities.c.status == "active",
        )
    )

    values: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(statement).mappings():
        membership_id = str(row["membership_id"])
        facility_key = str(row["facility_key"]).strip()
        if facility_key:
            values[membership_id].add(facility_key)
    return {
        membership_id: frozenset(facility_keys)
        for membership_id, facility_keys in values.items()
    }


def _user_from_row(row) -> UserCredential:
    return UserCredential(
        user_id=str(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        password_hash=row["password_hash"],
        password_algorithm=row["password_algorithm"],
        auth_version=int(row["auth_version"]),
    )


def _group_membership_rows(
    rows,
    facilities_by_membership: Mapping[str, frozenset[str]] | None = None,
) -> list[MembershipGrant]:
    facilities_by_membership = facilities_by_membership or {}
    permissions_by_membership: dict[str, set[PermissionCode]] = defaultdict(set)
    membership_facts: dict[str, object] = {}
    for row in rows:
        membership_id = str(row["membership_id"])
        membership_facts[membership_id] = row
        permission_key = row["permission_key"]
        if permission_key:
            try:
                permissions_by_membership[membership_id].add(PermissionCode(permission_key))
            except ValueError:
                continue

    return [
        MembershipGrant(
            membership_id=membership_id,
            organization_id=str(row["organization_id"]),
            organization_name=str(row["organization_name"]),
            status=str(row["status"]),
            authorization_version=int(row["authorization_version"]),
            permissions=frozenset(permissions_by_membership[membership_id]),
            allowed_facility_ids=facilities_by_membership.get(
                membership_id,
                frozenset(),
            ),
        )
        for membership_id, row in membership_facts.items()
    ]
