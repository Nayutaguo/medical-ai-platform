"""SQLAlchemy repository for identity credentials and durable sessions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from uuid import uuid4

from sqlalchemy import and_, create_engine, delete, func, or_, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from medical_ai.audit import sanitize_audit_details
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.authorization.errors import PermissionDeniedError
from medical_ai.config import Settings, get_settings
from medical_ai.db.schema import build_sqlalchemy_table
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
    AdministrationLastManagerError,
    AdministrationResourceNotFoundError,
    AdministrationScopeConflictError,
    AdministrationSelfLockoutError,
    AdministrationStatusConflictError,
    AdministrationVersionConflictError,
    BootstrapAlreadyCompletedError,
    FacilityCatalogInvalidError,
    FacilityCatalogScopeUnavailableError,
    FacilityOwnershipConflictError,
    AuthenticationRequiredError,
    InvalidInvitationError,
    InvitationCreationError,
)
from medical_ai.identity.models import (
    INVITATION_ACTIVATE_IDENTITY,
    INVITATION_ACTIVATE_MEMBERSHIP,
    INVITED_MEMBER_DISPLAY_NAME,
    ActiveSession,
    AdministrationFacility,
    AdministrationMember,
    AdministrationPage,
    AdministrationRole,
    BootstrapPlan,
    BootstrapResult,
    InvitationRegistrationChallenge,
    InvitationRegistrationPlan,
    FacilityCatalogSyncResult,
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
        ).where(
            users.c.email_normalized == normalized_email,
            users.c.deleted_at.is_(None),
        )
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
        *,
        _retry_on_conflict: bool = True,
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
                if plan.actor_kind == "user":
                    _revalidate_actor_snapshot(
                        connection,
                        user_id=plan.actor_user_id,
                        organization_id=plan.organization_id,
                        membership_id=plan.actor_membership_id,
                        identity_version=plan.actor_identity_version,
                        authorization_version=plan.actor_authorization_version,
                        session_id=plan.actor_session_id,
                        occurred_at=plan.created_at,
                        required_any=(PermissionCode.USERS_MANAGE,),
                    )
                elif plan.actor_kind != "system":
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
                    if not _user_can_receive_invitation(existing_user):
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
                    if existing_membership is None:
                        membership_id = plan.membership_id
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
                    elif existing_membership["status"] != "invited":
                        raise InvitationCreationError
                    else:
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
            if _retry_on_conflict:
                return self.create_user_invitation(
                    plan,
                    _retry_on_conflict=False,
                )
            raise InvitationCreationError from None
        except OperationalError as exc:
            if _is_retryable_invitation_lock_error(exc):
                if _retry_on_conflict:
                    return self.create_user_invitation(
                        plan,
                        _retry_on_conflict=False,
                    )
                raise InvitationCreationError from None
            raise

    def find_invitation_registration_challenge(
        self,
        *,
        token_hash: bytes,
        email_normalized: str,
        now: datetime,
    ) -> InvitationRegistrationChallenge | None:
        """Resolve internal credential proof facts for one usable invitation."""

        statement = _invitation_registration_statement(token_hash)
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return _invitation_registration_challenge(
            row,
            email_normalized=email_normalized,
            now=now,
        )

    def consume_user_invitation(
        self,
        plan: InvitationRegistrationPlan,
    ) -> RegistrationResult:
        """Consume one exact invitation and activate its identity atomically."""

        statement = _invitation_registration_statement(
            plan.token_hash,
            for_update=True,
        )
        token_organization_statement = select(
            one_time_tokens.c.organization_id
        ).where(
            one_time_tokens.c.token_hash == plan.token_hash,
            one_time_tokens.c.purpose == USER_INVITATION_PURPOSE,
        )

        try:
            with self.engine.begin() as connection:
                token_organization_id = connection.execute(
                    token_organization_statement
                ).scalar_one_or_none()
                if token_organization_id is None or not _lock_active_organization(
                    connection,
                    str(token_organization_id),
                ):
                    raise InvalidInvitationError
                row = connection.execute(statement).mappings().one_or_none()
                if not _registration_context_is_valid(row, plan):
                    raise InvalidInvitationError

                token_update = connection.execute(
                    update(one_time_tokens)
                    .where(
                        one_time_tokens.c.id == row["token_id"],
                        one_time_tokens.c.consumed_at.is_(None),
                        one_time_tokens.c.expires_at > plan.completed_at,
                        one_time_tokens.c.identity_version
                        == plan.expected_identity_version,
                        one_time_tokens.c.version == plan.expected_token_version,
                    )
                    .values(
                        consumed_at=plan.completed_at,
                        version=one_time_tokens.c.version + 1,
                    )
                )
                user_update_count = 1
                result_display_name = str(row["display_name"])
                if plan.activation_mode == INVITATION_ACTIVATE_IDENTITY:
                    user_update = connection.execute(
                        update(users)
                        .where(
                            users.c.id == row["user_id"],
                            users.c.email_normalized == plan.email_normalized,
                            users.c.status == "invited",
                            users.c.deleted_at.is_(None),
                            users.c.password_hash.is_(None),
                            users.c.password_algorithm.is_(None),
                            users.c.auth_version
                            == plan.expected_identity_version,
                            users.c.version == plan.expected_user_version,
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
                    user_update_count = user_update.rowcount
                    result_display_name = plan.display_name
                membership_update = connection.execute(
                    update(organization_memberships)
                    .where(
                        organization_memberships.c.id == row["membership_id"],
                        organization_memberships.c.user_id == row["user_id"],
                        organization_memberships.c.organization_id
                        == row["organization_id"],
                        organization_memberships.c.status == "invited",
                        organization_memberships.c.version
                        == plan.expected_membership_version,
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
                    or user_update_count != 1
                    or membership_update.rowcount != 1
                ):
                    raise InvalidInvitationError

                if plan.activation_mode == INVITATION_ACTIVATE_IDENTITY:
                    # Other organizations may already have issued invitations
                    # while this global identity was still pending.  Rebase
                    # only still-usable digests to the newly established
                    # credential version. They will subsequently require the
                    # existing password and can no longer reset the identity.
                    connection.execute(
                        update(one_time_tokens)
                        .where(
                            one_time_tokens.c.user_id == row["user_id"],
                            one_time_tokens.c.purpose == USER_INVITATION_PURPOSE,
                            one_time_tokens.c.consumed_at.is_(None),
                            one_time_tokens.c.expires_at > plan.completed_at,
                            one_time_tokens.c.identity_version
                            == plan.expected_identity_version,
                        )
                        .values(
                            identity_version=plan.expected_identity_version + 1,
                            version=one_time_tokens.c.version + 1,
                        )
                    )

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
                    display_name=result_display_name,
                )
        except IntegrityError:
            raise InvalidInvitationError from None
        except OperationalError as exc:
            # Two outstanding invitations for one global identity can race on
            # different tenant/token locks while first activation rebases the
            # sibling token.  Never replay an already verified password flow;
            # expose the rolled-back lock victim as the same unusable-
            # invitation outcome and let the caller explicitly retry.
            if _is_retryable_invitation_lock_error(exc):
                raise InvalidInvitationError from None
            raise

    def list_active_memberships(self, user_id: str) -> list[MembershipGrant]:
        """Resolve active organizations and explicit permission grants."""

        membership_join = (
            organization_memberships.join(
                organizations,
                organizations.c.id == organization_memberships.c.organization_id,
            )
            .join(users, users.c.id == organization_memberships.c.user_id)
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
                users.c.status == "active",
                users.c.deleted_at.is_(None),
                organizations.c.status == "active",
                organizations.c.deleted_at.is_(None),
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
                users.c.deleted_at.is_(None),
                organizations.c.status == "active",
                organizations.c.deleted_at.is_(None),
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
            session_id=str(row["session_id"]),
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

    def list_administration_members(
        self,
        organization_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        """Return a bounded keyset page of tenant members and explicit grants."""

        statement = (
            select(
                organization_memberships.c.id.label("membership_id"),
                organization_memberships.c.user_id,
                organization_memberships.c.status.label("membership_status"),
                organization_memberships.c.authorization_version,
                organization_memberships.c.version,
                users.c.email,
                users.c.email_normalized,
                users.c.display_name,
                users.c.status.label("user_status"),
            )
            .select_from(
                organization_memberships.join(
                    users,
                    users.c.id == organization_memberships.c.user_id,
                )
            )
            .where(organization_memberships.c.organization_id == organization_id)
            .order_by(organization_memberships.c.id)
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.where(organization_memberships.c.id > cursor)

        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            membership_ids = [str(row["membership_id"]) for row in page_rows]
            role_values = _administration_roles_by_membership(
                connection,
                organization_id,
                membership_ids,
            )
            facility_values = _administration_facilities_by_membership(
                connection,
                organization_id,
                membership_ids,
            )

        items = tuple(
            _administration_member_projection(
                row,
                roles=role_values.get(str(row["membership_id"]), ()),
                facilities=facility_values.get(str(row["membership_id"]), ()),
            )
            for row in page_rows
        )
        next_cursor = str(page_rows[-1]["membership_id"]) if has_more and page_rows else None
        return AdministrationPage(items=items, next_cursor=next_cursor)

    def list_administration_roles(
        self,
        organization_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        """Return roles owned by exactly one active administration tenant."""

        statement = (
            select(
                roles.c.id,
                roles.c.role_key,
                roles.c.name,
                roles.c.description,
                roles.c.version,
            )
            .where(roles.c.organization_id == organization_id)
            .order_by(roles.c.id)
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.where(roles.c.id > cursor)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            permission_values = _administration_permissions_by_role(
                connection,
                organization_id,
                [str(row["id"]) for row in page_rows],
            )
        items = tuple(
            _administration_role(row, permission_values) for row in page_rows
        )
        return AdministrationPage(
            items=items,
            next_cursor=str(page_rows[-1]["id"]) if has_more and page_rows else None,
        )

    def list_administration_facilities(
        self,
        organization_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        """Return the current organization's canonical facility catalog."""

        statement = (
            select(
                facilities.c.id,
                facilities.c.facility_key,
                facilities.c.display_name,
                facilities.c.status,
                facilities.c.version,
            )
            .select_from(
                organization_facilities.join(
                    facilities,
                    facilities.c.id == organization_facilities.c.facility_id,
                )
            )
            .where(organization_facilities.c.organization_id == organization_id)
            .order_by(facilities.c.id)
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.where(facilities.c.id > cursor)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = tuple(_administration_facility(row) for row in page_rows)
        return AdministrationPage(
            items=items,
            next_cursor=str(page_rows[-1]["id"]) if has_more and page_rows else None,
        )

    def replace_membership_roles(
        self,
        *,
        context: AccessContext,
        membership_id: str,
        role_ids: tuple[str, ...],
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationMember:
        """Replace role grants and append their audit fact in one transaction."""

        with self.engine.begin() as connection:
            _lock_administration_organization(connection, context.organization_id)
            _revalidate_access_context(
                connection,
                context,
                occurred_at=occurred_at,
                required_any=(PermissionCode.ROLES_ASSIGN,),
            )
            membership = _locked_membership(
                connection,
                context.organization_id,
                membership_id,
                expected_version,
            )
            selected_roles = _locked_organization_roles(
                connection,
                context.organization_id,
                role_ids,
            )
            previous_role_keys = _membership_role_keys(
                connection,
                context.organization_id,
                membership_id,
            )
            new_role_keys = tuple(
                sorted(
                    str(selected_roles[role_id]["role_key"])
                    for role_id in role_ids
                )
            )
            retains_management = _contains_management_role(
                connection,
                role_ids,
            )
            if membership_id == context.membership_id and not retains_management:
                raise AdministrationSelfLockoutError
            if (
                membership["status"] == "active"
                and not retains_management
                and _is_last_active_management_member(
                    connection,
                    context.organization_id,
                    membership_id,
                )
            ):
                raise AdministrationLastManagerError

            connection.execute(
                delete(membership_roles).where(
                    membership_roles.c.organization_id == context.organization_id,
                    membership_roles.c.membership_id == membership_id,
                )
            )
            if role_ids:
                connection.execute(
                    membership_roles.insert(),
                    [
                        {
                            "organization_id": context.organization_id,
                            "membership_id": membership_id,
                            "role_id": role_id,
                            "assigned_by_user_id": context.user_id,
                            "assigned_at": occurred_at,
                            "created_at": occurred_at,
                        }
                        for role_id in role_ids
                    ],
                )
            new_authorization_version = _advance_membership_version(
                connection,
                context.organization_id,
                membership_id,
                expected_version,
                int(membership["authorization_version"]),
                occurred_at,
            )
            _rebase_actor_sessions(
                connection,
                context=context,
                target_membership_id=membership_id,
                new_authorization_version=new_authorization_version,
                occurred_at=occurred_at,
            )
            _append_administration_audit(
                connection,
                context=context,
                request_id=request_id,
                occurred_at=occurred_at,
                action="identity.membership.roles.update",
                membership_id=membership_id,
                details={
                    "previous_role_keys": previous_role_keys,
                    "new_role_keys": new_role_keys,
                },
            )
            return _administration_member(
                connection,
                context.organization_id,
                membership_id,
            )

    def replace_membership_facility_scope(
        self,
        *,
        context: AccessContext,
        membership_id: str,
        facility_ids: tuple[str, ...],
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationMember:
        """Replace explicit facility scope and audit it in one transaction."""

        with self.engine.begin() as connection:
            _lock_administration_organization(connection, context.organization_id)
            _revalidate_access_context(
                connection,
                context,
                occurred_at=occurred_at,
                required_any=(PermissionCode.ROLES_ASSIGN,),
            )
            membership = _locked_membership(
                connection,
                context.organization_id,
                membership_id,
                expected_version,
            )
            _locked_organization_facilities(
                connection,
                context.organization_id,
                facility_ids,
            )
            previous_facility_ids = _membership_facility_ids(
                connection,
                context.organization_id,
                membership_id,
            )
            connection.execute(
                delete(membership_facility_scopes).where(
                    membership_facility_scopes.c.organization_id
                    == context.organization_id,
                    membership_facility_scopes.c.membership_id == membership_id,
                )
            )
            if facility_ids:
                connection.execute(
                    membership_facility_scopes.insert(),
                    [
                        {
                            "membership_id": membership_id,
                            "facility_id": facility_id,
                            "organization_id": context.organization_id,
                            "granted_by_user_id": context.user_id,
                            "granted_at": occurred_at,
                            "created_at": occurred_at,
                            "updated_at": occurred_at,
                        }
                        for facility_id in facility_ids
                    ],
                )
            new_authorization_version = _advance_membership_version(
                connection,
                context.organization_id,
                membership_id,
                expected_version,
                int(membership["authorization_version"]),
                occurred_at,
            )
            _rebase_actor_sessions(
                connection,
                context=context,
                target_membership_id=membership_id,
                new_authorization_version=new_authorization_version,
                occurred_at=occurred_at,
            )
            _append_administration_audit(
                connection,
                context=context,
                request_id=request_id,
                occurred_at=occurred_at,
                action="identity.membership.facility_scope.update",
                membership_id=membership_id,
                details={
                    "previous_facility_count": len(previous_facility_ids),
                    "new_facility_count": len(facility_ids),
                    "previous_facility_scope_digest": _identifier_set_digest(
                        previous_facility_ids
                    ),
                    "new_facility_scope_digest": _identifier_set_digest(facility_ids),
                },
            )
            return _administration_member(
                connection,
                context.organization_id,
                membership_id,
            )

    def update_membership_status(
        self,
        *,
        context: AccessContext,
        membership_id: str,
        status: str,
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationMember:
        """Change membership lifecycle state without deleting its user."""

        if membership_id == context.membership_id and status != "active":
            raise AdministrationSelfLockoutError
        with self.engine.begin() as connection:
            _lock_administration_organization(connection, context.organization_id)
            _revalidate_access_context(
                connection,
                context,
                occurred_at=occurred_at,
                required_any=(PermissionCode.USERS_MANAGE,),
            )
            membership = _locked_membership(
                connection,
                context.organization_id,
                membership_id,
                expected_version,
            )
            current_status = str(membership["status"])
            if current_status not in {"active", "suspended"}:
                raise AdministrationStatusConflictError
            if (
                current_status == "active"
                and status == "suspended"
                and _is_last_active_management_member(
                    connection,
                    context.organization_id,
                    membership_id,
                )
            ):
                raise AdministrationLastManagerError
            statement = (
                update(organization_memberships)
                .where(
                    organization_memberships.c.organization_id
                    == context.organization_id,
                    organization_memberships.c.id == membership_id,
                    organization_memberships.c.version == expected_version,
                )
                .values(
                    status=status,
                    authorization_version=
                    organization_memberships.c.authorization_version + 1,
                    updated_at=occurred_at,
                    version=organization_memberships.c.version + 1,
                )
            )
            if connection.execute(statement).rowcount != 1:
                raise AdministrationVersionConflictError
            new_authorization_version = int(membership["authorization_version"]) + 1
            _rebase_actor_sessions(
                connection,
                context=context,
                target_membership_id=membership_id,
                new_authorization_version=new_authorization_version,
                occurred_at=occurred_at,
            )
            _append_administration_audit(
                connection,
                context=context,
                request_id=request_id,
                occurred_at=occurred_at,
                action="identity.membership.status.update",
                membership_id=membership_id,
                details={
                    "previous_status": current_status,
                    "new_status": status,
                },
            )
            return _administration_member(
                connection,
                context.organization_id,
                membership_id,
            )

    def sync_organization_facilities(
        self,
        *,
        context: AccessContext,
        request_id: str | None,
        occurred_at: datetime,
    ) -> FacilityCatalogSyncResult:
        """Idempotently claim the bounded inpatient facility catalog.

        This first deployment uses one MySQL transaction because analytics and
        control tables share the injected engine. A future split-schema adapter
        must replace this method with a durable staged catalog handoff rather
        than pretending two independent databases commit atomically.
        """

        configured_owner = self.settings.inpatient_dataset_owner_organization_id.strip()
        if not configured_owner or configured_owner != context.organization_id:
            raise FacilityCatalogScopeUnavailableError

        inpatient = build_sqlalchemy_table("inpatient")
        source_id = inpatient.c.PermanentFacilityId
        source_name = inpatient.c.FacilityName
        catalog_statement = (
            select(
                source_id.label("facility_key"),
                func.max(source_name).label("display_name"),
            )
            .where(source_id.is_not(None), func.trim(source_id) != "")
            .group_by(source_id)
            .limit(10_001)
        )
        try:
            with self.engine.begin() as connection:
                _lock_administration_organization(
                    connection,
                    context.organization_id,
                )
                _revalidate_access_context(
                    connection,
                    context,
                    occurred_at=occurred_at,
                    required_any=(
                        PermissionCode.ROLES_ASSIGN,
                        PermissionCode.IMPORTS_CREATE,
                    ),
                )
                source_rows = connection.execute(catalog_statement).mappings().all()
                catalog = _normalized_facility_catalog(source_rows)
                keys = tuple(catalog)
                existing_rows = connection.execute(
                    select(
                        facilities.c.id,
                        facilities.c.facility_key,
                        facilities.c.display_name,
                    )
                    .where(facilities.c.facility_key.in_(keys))
                    .order_by(facilities.c.id)
                    .with_for_update()
                ).mappings().all() if keys else []
                existing_by_key = {
                    str(row["facility_key"]): row for row in existing_rows
                }
                existing_ids = [str(row["id"]) for row in existing_rows]
                owner_rows = connection.execute(
                    select(
                        organization_facilities.c.facility_id,
                        organization_facilities.c.organization_id,
                    )
                    .where(organization_facilities.c.facility_id.in_(existing_ids))
                    .order_by(organization_facilities.c.facility_id)
                    .with_for_update()
                ).mappings().all() if existing_ids else []
                owners = {
                    str(row["facility_id"]): str(row["organization_id"])
                    for row in owner_rows
                }
                if any(
                    owners.get(str(row["id"])) not in (None, context.organization_id)
                    for row in existing_rows
                ):
                    raise FacilityOwnershipConflictError

                created_count = 0
                existing_count = 0
                for facility_key, display_name in catalog.items():
                    existing = existing_by_key.get(facility_key)
                    if existing is None:
                        facility_id = str(uuid4())
                        try:
                            connection.execute(
                                facilities.insert().values(
                                    id=facility_id,
                                    facility_key=facility_key,
                                    display_name=display_name,
                                    status="active",
                                    created_at=occurred_at,
                                    updated_at=occurred_at,
                                )
                            )
                        except IntegrityError:
                            raise FacilityOwnershipConflictError from None
                        created_count += 1
                    else:
                        facility_id = str(existing["id"])
                        existing_count += 1
                        if display_name and display_name != existing["display_name"]:
                            connection.execute(
                                update(facilities)
                                .where(facilities.c.id == facility_id)
                                .values(
                                    display_name=display_name,
                                    updated_at=occurred_at,
                                    version=facilities.c.version + 1,
                                )
                            )

                    if owners.get(facility_id) is None:
                        try:
                            connection.execute(
                                organization_facilities.insert().values(
                                    organization_id=context.organization_id,
                                    facility_id=facility_id,
                                    granted_by_user_id=context.user_id,
                                    granted_at=occurred_at,
                                    created_at=occurred_at,
                                    updated_at=occurred_at,
                                )
                            )
                        except IntegrityError:
                            raise FacilityOwnershipConflictError from None

                connection.execute(
                    audit_events.insert().values(
                        occurred_at=occurred_at,
                        request_id=request_id,
                        organization_id=context.organization_id,
                        actor_kind="user",
                        actor_membership_id=context.membership_id,
                        actor_user_id=context.user_id,
                        action="identity.facility_catalog.sync",
                        resource_type="facility_catalog",
                        resource_id=context.organization_id,
                        outcome="success",
                        details=sanitize_audit_details(
                            {
                                "created_count": created_count,
                                "existing_count": existing_count,
                            }
                        ),
                    )
                )
                return FacilityCatalogSyncResult(
                    created_count=created_count,
                    existing_count=existing_count,
                )
        except FacilityOwnershipConflictError:
            raise


def _locked_membership(
    connection: Connection,
    organization_id: str,
    membership_id: str,
    expected_version: int,
) -> Mapping[str, object]:
    row = connection.execute(
        select(
            organization_memberships.c.id,
            organization_memberships.c.status,
            organization_memberships.c.authorization_version,
            organization_memberships.c.version,
        )
        .where(
            organization_memberships.c.organization_id == organization_id,
            organization_memberships.c.id == membership_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if row is None:
        raise AdministrationResourceNotFoundError
    if int(row["version"]) != expected_version:
        raise AdministrationVersionConflictError
    return row


def _lock_active_organization(
    connection: Connection,
    organization_id: str,
) -> bool:
    """Lock one active tenant row as the control-plane mutation mutex."""

    locked_id = connection.execute(
        select(organizations.c.id)
        .where(
            organizations.c.id == organization_id,
            organizations.c.status == "active",
            organizations.c.deleted_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    return locked_id is not None


def _lock_administration_organization(
    connection: Connection,
    organization_id: str,
) -> None:
    if not _lock_active_organization(connection, organization_id):
        raise AdministrationResourceNotFoundError


def _revalidate_access_context(
    connection: Connection,
    context: AccessContext,
    *,
    occurred_at: datetime,
    required_any: tuple[PermissionCode, ...],
) -> None:
    _revalidate_actor_snapshot(
        connection,
        user_id=context.user_id,
        organization_id=context.organization_id,
        membership_id=context.membership_id,
        identity_version=context.identity_version,
        authorization_version=context.authorization_version,
        session_id=context.session_id,
        occurred_at=occurred_at,
        required_any=required_any,
    )


def _revalidate_actor_snapshot(
    connection: Connection,
    *,
    user_id: str | None,
    organization_id: str,
    membership_id: str | None,
    identity_version: int | None,
    authorization_version: int | None,
    session_id: str | None,
    occurred_at: datetime,
    required_any: tuple[PermissionCode, ...],
) -> None:
    """Recheck a user actor after the tenant write mutex is acquired."""

    if (
        not isinstance(user_id, str)
        or not user_id
        or not isinstance(membership_id, str)
        or not membership_id
        or isinstance(identity_version, bool)
        or not isinstance(identity_version, int)
        or isinstance(authorization_version, bool)
        or not isinstance(authorization_version, int)
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise AuthenticationRequiredError
    session_exists = connection.execute(
        select(auth_sessions.c.id)
        .where(
            auth_sessions.c.id == session_id,
            auth_sessions.c.membership_id == membership_id,
            auth_sessions.c.user_id == user_id,
            auth_sessions.c.organization_id == organization_id,
            auth_sessions.c.identity_version == identity_version,
            auth_sessions.c.authorization_version == authorization_version,
            auth_sessions.c.revoked_at.is_(None),
            auth_sessions.c.expires_at > occurred_at,
            auth_sessions.c.idle_expires_at > occurred_at,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if session_exists is None:
        raise AuthenticationRequiredError
    row = connection.execute(
        select(
            users.c.status.label("user_status"),
            users.c.deleted_at.label("user_deleted_at"),
            users.c.auth_version,
            organization_memberships.c.status.label("membership_status"),
            organization_memberships.c.authorization_version,
        )
        .select_from(
            organization_memberships.join(
                users,
                users.c.id == organization_memberships.c.user_id,
            )
        )
        .where(
            organization_memberships.c.id == membership_id,
            organization_memberships.c.user_id == user_id,
            organization_memberships.c.organization_id == organization_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if (
        row is None
        or row["user_status"] != "active"
        or row["user_deleted_at"] is not None
        or row["membership_status"] != "active"
        or int(row["auth_version"]) != identity_version
        or int(row["authorization_version"]) != authorization_version
    ):
        raise AuthenticationRequiredError
    current_permissions = _permission_values(connection, membership_id)
    if not any(permission in current_permissions for permission in required_any):
        raise PermissionDeniedError(
            "|".join(permission.value for permission in required_any)
        )


def _locked_organization_roles(
    connection: Connection,
    organization_id: str,
    role_ids: tuple[str, ...],
) -> dict[str, Mapping[str, object]]:
    if not role_ids:
        return {}
    rows = connection.execute(
        select(roles.c.id, roles.c.role_key)
        .where(
            roles.c.organization_id == organization_id,
            roles.c.id.in_(role_ids),
        )
        .with_for_update()
    ).mappings().all()
    result = {str(row["id"]): row for row in rows}
    if set(result) != set(role_ids):
        raise AdministrationScopeConflictError
    return result


def _locked_organization_facilities(
    connection: Connection,
    organization_id: str,
    facility_ids: tuple[str, ...],
) -> None:
    if not facility_ids:
        return
    rows = connection.execute(
        select(organization_facilities.c.facility_id)
        .select_from(
            organization_facilities.join(
                facilities,
                facilities.c.id == organization_facilities.c.facility_id,
            )
        )
        .where(
            organization_facilities.c.organization_id == organization_id,
            organization_facilities.c.facility_id.in_(facility_ids),
            facilities.c.status == "active",
        )
        .with_for_update()
    ).scalars().all()
    if {str(value) for value in rows} != set(facility_ids):
        raise AdministrationScopeConflictError


def _membership_role_keys(
    connection: Connection,
    organization_id: str,
    membership_id: str,
) -> tuple[str, ...]:
    values = connection.execute(
        select(roles.c.role_key)
        .select_from(
            membership_roles.join(roles, roles.c.id == membership_roles.c.role_id)
        )
        .where(
            membership_roles.c.organization_id == organization_id,
            membership_roles.c.membership_id == membership_id,
            roles.c.organization_id == organization_id,
        )
        .order_by(roles.c.role_key)
    ).scalars().all()
    return tuple(str(value) for value in values)


def _membership_facility_ids(
    connection: Connection,
    organization_id: str,
    membership_id: str,
) -> tuple[str, ...]:
    values = connection.execute(
        select(membership_facility_scopes.c.facility_id)
        .where(
            membership_facility_scopes.c.organization_id == organization_id,
            membership_facility_scopes.c.membership_id == membership_id,
        )
        .order_by(membership_facility_scopes.c.facility_id)
    ).scalars().all()
    return tuple(str(value) for value in values)


def _identifier_set_digest(values: tuple[str, ...]) -> str:
    """Hash a sorted identifier set without persisting its raw values."""

    payload = "\n".join(sorted(values)).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def _contains_management_role(
    connection: Connection,
    role_ids: tuple[str, ...],
) -> bool:
    """Require the selected roles' permission union to retain administration."""

    if not role_ids:
        return False
    rows = connection.execute(
        select(permissions.c.permission_key)
        .select_from(
            role_permissions.join(
                permissions,
                permissions.c.id == role_permissions.c.permission_id,
            )
        )
        .where(
            role_permissions.c.role_id.in_(role_ids),
            permissions.c.permission_key.in_(
                (
                    PermissionCode.USERS_MANAGE.value,
                    PermissionCode.ROLES_ASSIGN.value,
                )
            ),
        )
    ).scalars().all()
    selected_permissions = {str(value) for value in rows}
    required = {
        PermissionCode.USERS_MANAGE.value,
        PermissionCode.ROLES_ASSIGN.value,
    }
    return required.issubset(selected_permissions)


def _is_last_active_management_member(
    connection: Connection,
    organization_id: str,
    membership_id: str,
) -> bool:
    """Return whether the target is the tenant's only active full manager."""

    rows = connection.execute(
        select(membership_roles.c.membership_id)
        .select_from(
            membership_roles.join(
                organization_memberships,
                and_(
                    organization_memberships.c.id
                    == membership_roles.c.membership_id,
                    organization_memberships.c.organization_id
                    == membership_roles.c.organization_id,
                ),
            )
            .join(
                roles,
                and_(
                    roles.c.id == membership_roles.c.role_id,
                    roles.c.organization_id == membership_roles.c.organization_id,
                ),
            )
            .join(
                role_permissions,
                role_permissions.c.role_id == membership_roles.c.role_id,
            )
            .join(
                permissions,
                permissions.c.id == role_permissions.c.permission_id,
            )
            .join(users, users.c.id == organization_memberships.c.user_id)
        )
        .where(
            membership_roles.c.organization_id == organization_id,
            organization_memberships.c.status == "active",
            users.c.status == "active",
            users.c.deleted_at.is_(None),
            permissions.c.permission_key.in_(
                (
                    PermissionCode.USERS_MANAGE.value,
                    PermissionCode.ROLES_ASSIGN.value,
                )
            ),
        )
        .group_by(membership_roles.c.membership_id)
        .having(func.count(func.distinct(permissions.c.permission_key)) == 2)
    ).scalars().all()
    active_manager_ids = {str(value) for value in rows}
    return active_manager_ids == {membership_id}


def _advance_membership_version(
    connection: Connection,
    organization_id: str,
    membership_id: str,
    expected_version: int,
    current_authorization_version: int,
    occurred_at: datetime,
) -> int:
    statement = (
        update(organization_memberships)
        .where(
            organization_memberships.c.organization_id == organization_id,
            organization_memberships.c.id == membership_id,
            organization_memberships.c.version == expected_version,
        )
        .values(
            authorization_version=organization_memberships.c.authorization_version + 1,
            updated_at=occurred_at,
            version=organization_memberships.c.version + 1,
        )
    )
    if connection.execute(statement).rowcount != 1:
        raise AdministrationVersionConflictError
    return current_authorization_version + 1


def _rebase_actor_sessions(
    connection: Connection,
    *,
    context: AccessContext,
    target_membership_id: str,
    new_authorization_version: int,
    occurred_at: datetime,
) -> None:
    """Keep only the exact requesting session alive after a self mutation."""

    if target_membership_id != context.membership_id:
        return
    if context.session_id is None:
        raise AuthenticationRequiredError
    statement = (
        update(auth_sessions)
        .where(
            auth_sessions.c.id == context.session_id,
            auth_sessions.c.membership_id == context.membership_id,
            auth_sessions.c.user_id == context.user_id,
            auth_sessions.c.organization_id == context.organization_id,
            auth_sessions.c.identity_version == context.identity_version,
            auth_sessions.c.authorization_version == context.authorization_version,
            auth_sessions.c.revoked_at.is_(None),
            auth_sessions.c.expires_at > occurred_at,
            auth_sessions.c.idle_expires_at > occurred_at,
        )
        .values(
            authorization_version=new_authorization_version,
            updated_at=occurred_at,
            version=auth_sessions.c.version + 1,
        )
    )
    if connection.execute(statement).rowcount != 1:
        raise AuthenticationRequiredError


def _administration_member(
    connection: Connection,
    organization_id: str,
    membership_id: str,
) -> AdministrationMember:
    """Read the committed-to-be tenant member representation inside a write transaction."""

    row = connection.execute(
        select(
            organization_memberships.c.id.label("membership_id"),
            organization_memberships.c.user_id,
            organization_memberships.c.status.label("membership_status"),
            organization_memberships.c.authorization_version,
            organization_memberships.c.version,
            users.c.email,
            users.c.email_normalized,
            users.c.display_name,
            users.c.status.label("user_status"),
        )
        .select_from(
            organization_memberships.join(
                users,
                users.c.id == organization_memberships.c.user_id,
            )
        )
        .where(
            organization_memberships.c.organization_id == organization_id,
            organization_memberships.c.id == membership_id,
        )
    ).mappings().one_or_none()
    if row is None:
        raise AdministrationResourceNotFoundError
    role_values = _administration_roles_by_membership(
        connection,
        organization_id,
        [membership_id],
    )
    facility_values = _administration_facilities_by_membership(
        connection,
        organization_id,
        [membership_id],
    )
    return _administration_member_projection(
        row,
        roles=role_values.get(membership_id, ()),
        facilities=facility_values.get(membership_id, ()),
    )


def _administration_member_projection(
    row: Mapping[str, object],
    *,
    roles: tuple[AdministrationRole, ...],
    facilities: tuple[AdministrationFacility, ...],
) -> AdministrationMember:
    """Hide global identity facts while a tenant membership is invited."""

    membership_status = str(row["membership_status"])
    invited = membership_status == "invited"
    return AdministrationMember(
        membership_id=str(row["membership_id"]),
        user_id=str(row["user_id"]),
        email=(
            str(row["email_normalized"])
            if invited
            else str(row["email"])
        ),
        display_name=(
            INVITED_MEMBER_DISPLAY_NAME
            if invited
            else str(row["display_name"])
        ),
        user_status="invited" if invited else str(row["user_status"]),
        membership_status=membership_status,
        authorization_version=int(row["authorization_version"]),
        version=int(row["version"]),
        roles=roles,
        facilities=facilities,
    )


def _append_administration_audit(
    connection: Connection,
    *,
    context: AccessContext,
    request_id: str | None,
    occurred_at: datetime,
    action: str,
    membership_id: str,
    details: Mapping[str, object],
) -> None:
    connection.execute(
        audit_events.insert().values(
            occurred_at=occurred_at,
            request_id=request_id,
            organization_id=context.organization_id,
            actor_kind="user",
            actor_membership_id=context.membership_id,
            actor_user_id=context.user_id,
            action=action,
            resource_type="membership",
            resource_id=membership_id,
            outcome="success",
            details=sanitize_audit_details(details),
        )
    )


def _administration_roles_by_membership(
    connection: Connection,
    organization_id: str,
    membership_ids: list[str],
) -> dict[str, tuple[AdministrationRole, ...]]:
    if not membership_ids:
        return {}
    rows = connection.execute(
        select(
            membership_roles.c.membership_id,
            roles.c.id,
            roles.c.role_key,
            roles.c.name,
            roles.c.description,
            roles.c.version,
        )
        .select_from(
            membership_roles.join(roles, roles.c.id == membership_roles.c.role_id)
        )
        .where(
            membership_roles.c.organization_id == organization_id,
            membership_roles.c.membership_id.in_(membership_ids),
            roles.c.organization_id == organization_id,
        )
        .order_by(membership_roles.c.membership_id, roles.c.id)
    ).mappings().all()
    permission_values = _administration_permissions_by_role(
        connection,
        organization_id,
        sorted({str(row["id"]) for row in rows}),
    )
    grouped: dict[str, list[AdministrationRole]] = defaultdict(list)
    for row in rows:
        grouped[str(row["membership_id"])].append(
            _administration_role(row, permission_values)
        )
    return {key: tuple(values) for key, values in grouped.items()}


def _administration_facilities_by_membership(
    connection: Connection,
    organization_id: str,
    membership_ids: list[str],
) -> dict[str, tuple[AdministrationFacility, ...]]:
    if not membership_ids:
        return {}
    rows = connection.execute(
        select(
            membership_facility_scopes.c.membership_id,
            facilities.c.id,
            facilities.c.facility_key,
            facilities.c.display_name,
            facilities.c.status,
            facilities.c.version,
        )
        .select_from(
            membership_facility_scopes.join(
                facilities,
                facilities.c.id == membership_facility_scopes.c.facility_id,
            )
        )
        .where(
            membership_facility_scopes.c.organization_id == organization_id,
            membership_facility_scopes.c.membership_id.in_(membership_ids),
        )
        .order_by(membership_facility_scopes.c.membership_id, facilities.c.id)
    ).mappings().all()
    grouped: dict[str, list[AdministrationFacility]] = defaultdict(list)
    for row in rows:
        grouped[str(row["membership_id"])].append(_administration_facility(row))
    return {key: tuple(values) for key, values in grouped.items()}


def _administration_facility(row: Mapping[str, object]) -> AdministrationFacility:
    display_name = row["display_name"]
    return AdministrationFacility(
        id=str(row["id"]),
        facility_key=str(row["facility_key"]),
        display_name=str(display_name) if display_name is not None else None,
        status=str(row["status"]),
        version=int(row["version"]),
    )


def _administration_role(
    row: Mapping[str, object],
    permission_values: Mapping[str, tuple[str, ...]],
) -> AdministrationRole:
    description = row["description"]
    role_id = str(row["id"])
    return AdministrationRole(
        id=role_id,
        role_key=str(row["role_key"]),
        name=str(row["name"]),
        description=str(description) if description is not None else None,
        permissions=permission_values.get(role_id, ()),
        version=int(row["version"]),
    )


def _administration_permissions_by_role(
    connection: Connection,
    organization_id: str,
    role_ids: list[str],
) -> dict[str, tuple[str, ...]]:
    """Resolve role permissions separately so list rows cannot multiply."""

    if not role_ids:
        return {}
    rows = connection.execute(
        select(role_permissions.c.role_id, permissions.c.permission_key)
        .select_from(
            role_permissions.join(
                permissions,
                permissions.c.id == role_permissions.c.permission_id,
            ).join(roles, roles.c.id == role_permissions.c.role_id)
        )
        .where(
            roles.c.organization_id == organization_id,
            role_permissions.c.role_id.in_(role_ids),
        )
        .order_by(role_permissions.c.role_id, permissions.c.permission_key)
    ).mappings().all()
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["role_id"])].append(str(row["permission_key"]))
    return {key: tuple(values) for key, values in grouped.items()}


def _normalized_facility_catalog(
    rows: list[Mapping[str, object]],
) -> dict[str, str | None]:
    if len(rows) > 10_000:
        raise FacilityCatalogInvalidError
    catalog: dict[str, str | None] = {}
    for row in rows:
        raw_key = row["facility_key"]
        if not isinstance(raw_key, str):
            raise FacilityCatalogInvalidError
        facility_key = raw_key.strip()
        if not facility_key:
            continue
        if len(facility_key) > 128:
            raise FacilityCatalogInvalidError
        raw_name = row["display_name"]
        if raw_name is not None and not isinstance(raw_name, str):
            raise FacilityCatalogInvalidError
        display_name = raw_name.strip() if isinstance(raw_name, str) else None
        if display_name == "":
            display_name = None
        if display_name is not None and len(display_name) > 200:
            raise FacilityCatalogInvalidError
        previous = catalog.get(facility_key)
        if previous is None or (display_name is not None and display_name > previous):
            catalog[facility_key] = display_name
    return dict(sorted(catalog.items()))


def _invitation_registration_statement(
    token_hash: bytes,
    *,
    for_update: bool = False,
):
    """Build the single token-bound identity context query."""

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
            users.c.display_name,
            users.c.password_hash,
            users.c.password_algorithm,
            users.c.status.label("user_status"),
            users.c.auth_version,
            users.c.version.label("user_version"),
            users.c.deleted_at.label("user_deleted_at"),
            organization_memberships.c.status.label("membership_status"),
            organization_memberships.c.version.label("membership_version"),
            organizations.c.status.label("organization_status"),
            organizations.c.deleted_at.label("organization_deleted_at"),
        )
        .select_from(invitation_context)
        .where(
            one_time_tokens.c.token_hash == token_hash,
            one_time_tokens.c.purpose == USER_INVITATION_PURPOSE,
        )
    )
    return statement.with_for_update() if for_update else statement


def _invitation_registration_challenge(
    row: Mapping[str, object] | None,
    *,
    email_normalized: str,
    now: datetime,
) -> InvitationRegistrationChallenge | None:
    """Return one non-public acceptance mode without revealing failures."""

    if row is None:
        return None
    bound_email = row["email_normalized"]
    expires_at = row["expires_at"]
    if not bool(
        isinstance(bound_email, str)
        and compare_digest(
            bound_email.encode("utf-8"),
            email_normalized.encode("utf-8"),
        )
        and row["consumed_at"] is None
        and isinstance(expires_at, datetime)
        and expires_at > now
        and row["membership_status"] == "invited"
        and row["organization_status"] == "active"
        and row["organization_deleted_at"] is None
        and row["user_deleted_at"] is None
        and int(row["token_identity_version"]) == int(row["auth_version"])
    ):
        return None

    if (
        row["user_status"] == "invited"
        and row["password_hash"] is None
        and row["password_algorithm"] is None
    ):
        activation_mode = INVITATION_ACTIVATE_IDENTITY
    elif (
        row["user_status"] == "active"
        and isinstance(row["password_hash"], str)
        and bool(row["password_hash"])
        and isinstance(row["password_algorithm"], str)
        and bool(row["password_algorithm"])
    ):
        activation_mode = INVITATION_ACTIVATE_MEMBERSHIP
    else:
        return None

    return InvitationRegistrationChallenge(
        activation_mode=activation_mode,
        password_hash=(
            str(row["password_hash"])
            if isinstance(row["password_hash"], str)
            else None
        ),
        password_algorithm=(
            str(row["password_algorithm"])
            if isinstance(row["password_algorithm"], str)
            else None
        ),
        identity_version=int(row["auth_version"]),
        user_version=int(row["user_version"]),
        token_version=int(row["token_version"]),
        membership_version=int(row["membership_version"]),
    )


def _registration_context_is_valid(
    row: Mapping[str, object] | None,
    plan: InvitationRegistrationPlan,
) -> bool:
    """Validate every invitation binding without exposing which fact failed."""

    challenge = _invitation_registration_challenge(
        row,
        email_normalized=plan.email_normalized,
        now=plan.completed_at,
    )
    if challenge is None:
        return False
    if not (
        challenge.activation_mode == plan.activation_mode
        and challenge.identity_version == plan.expected_identity_version
        and challenge.user_version == plan.expected_user_version
        and challenge.token_version == plan.expected_token_version
        and challenge.membership_version == plan.expected_membership_version
        and challenge.password_algorithm == plan.expected_password_algorithm
    ):
        return False
    if challenge.password_hash is None or plan.expected_password_hash is None:
        hashes_match = challenge.password_hash is plan.expected_password_hash
    else:
        hashes_match = compare_digest(
            challenge.password_hash,
            plan.expected_password_hash,
        )
    if not hashes_match:
        return False
    if plan.activation_mode == INVITATION_ACTIVATE_IDENTITY:
        return bool(
            isinstance(plan.password_hash, str)
            and bool(plan.password_hash)
            and plan.password_algorithm == "argon2id"
        )
    return bool(
        plan.activation_mode == INVITATION_ACTIVATE_MEMBERSHIP
        and plan.password_hash is None
        and plan.password_algorithm is None
    )


def _user_can_receive_invitation(row: Mapping[str, object]) -> bool:
    """Allow a healthy global identity to join another organization."""

    if row["deleted_at"] is not None or int(row["auth_version"]) < 1:
        return False
    if row["status"] == "invited":
        return bool(
            row["password_hash"] is None
            and row["password_algorithm"] is None
        )
    return bool(
        row["status"] == "active"
        and isinstance(row["password_hash"], str)
        and bool(row["password_hash"])
        and isinstance(row["password_algorithm"], str)
        and bool(row["password_algorithm"])
    )


def _is_retryable_invitation_lock_error(exc: OperationalError) -> bool:
    """Recognize MySQL lock timeout/deadlock codes without masking outages."""

    original_args = getattr(exc.orig, "args", ())
    if not original_args:
        return False
    try:
        error_code = int(original_args[0])
    except (TypeError, ValueError):
        return False
    return error_code in {1205, 1213}


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
