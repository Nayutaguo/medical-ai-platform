from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Integer, create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from medical_ai.audit import AuditActor
from medical_ai.config import Settings
from medical_ai.identity import (
    audit_events,
    identity_metadata,
    one_time_tokens,
    organization_memberships,
    organizations,
    users,
)
from medical_ai.identity.errors import InvalidInvitationError, InvitationCreationError
from medical_ai.identity.models import (
    INVITATION_ACTIVATE_IDENTITY,
    INVITATION_ACTIVATE_MEMBERSHIP,
    INVITED_MEMBER_DISPLAY_NAME,
    InvitationRegistrationChallenge,
    InvitationRegistrationPlan,
    PersistedUserInvitation,
    RegistrationResult,
    UserInvitationPlan,
)
from medical_ai.identity.passwords import Argon2idPasswordService, PasswordPolicyError
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.repositories import IdentityRepository
from medical_ai.services import InvitationRegistrationService


class FakeInvitationRepository:
    def __init__(self) -> None:
        self.invitation_plan: UserInvitationPlan | None = None
        self.registration_plan: InvitationRegistrationPlan | None = None
        self.registration_error: Exception | None = None
        self.challenge_email_normalized = "invitee@example.com"
        self.challenge: InvitationRegistrationChallenge | None = (
            InvitationRegistrationChallenge(
                activation_mode=INVITATION_ACTIVATE_IDENTITY,
                password_hash=None,
                password_algorithm=None,
                identity_version=1,
                user_version=1,
                token_version=1,
                membership_version=1,
            )
        )

    def create_user_invitation(
        self,
        plan: UserInvitationPlan,
    ) -> PersistedUserInvitation:
        self.invitation_plan = plan
        return PersistedUserInvitation(
            user_id=plan.user_id,
            organization_id=plan.organization_id,
            membership_id=plan.membership_id,
        )

    def find_invitation_registration_challenge(
        self,
        *,
        token_hash: bytes,
        email_normalized: str,
        now: datetime,
    ) -> InvitationRegistrationChallenge | None:
        del token_hash, now
        if email_normalized != self.challenge_email_normalized:
            return None
        return self.challenge

    def consume_user_invitation(
        self,
        plan: InvitationRegistrationPlan,
    ) -> RegistrationResult:
        self.registration_plan = plan
        if self.registration_error:
            raise self.registration_error
        return RegistrationResult(
            user_id="user-1",
            organization_id="organization-1",
            membership_id="membership-1",
            email="invitee@example.com",
            display_name=plan.display_name,
        )


@pytest.fixture
def password_service() -> Argon2idPasswordService:
    return Argon2idPasswordService(time_cost=1, memory_cost_kib=8_192, parallelism=1)


def test_issue_invitation_returns_raw_token_once_but_persists_only_digest() -> None:
    repository = FakeInvitationRepository()
    service = InvitationRegistrationService(repository)
    organization_id = str(uuid4())
    current_time = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)

    invitation = service.issue_invitation(
        organization_id=organization_id,
        email=" Invitee@Example.com ",
        actor=AuditActor.system(organization_id),
        lifetime=timedelta(hours=12),
        request_id="request-1",
        now=current_time,
    )

    plan = repository.invitation_plan
    assert plan is not None
    assert plan.email_normalized == "invitee@example.com"
    assert plan.organization_id == organization_id
    assert plan.actor_kind == "system"
    assert plan.actor_user_id is None
    assert plan.identity_version == 1
    assert plan.token_hash == hash_opaque_token(invitation.token)
    assert invitation.token not in repr(invitation)
    assert invitation.token not in repr(plan)
    assert plan.expires_at == datetime(2026, 8, 24, 20, 0)


def test_issue_invitation_requires_same_tenant_audit_actor() -> None:
    repository = FakeInvitationRepository()
    service = InvitationRegistrationService(repository)

    with pytest.raises(ValueError, match="actor"):
        service.issue_invitation(
            organization_id=str(uuid4()),
            email="invitee@example.com",
            actor=AuditActor.system(str(uuid4())),
        )

    assert repository.invitation_plan is None


@pytest.mark.parametrize(
    "email",
    ["ü@example.com", "josé@example.com", "K@example.com", "ſ@example.com", "ß@example.com"],
)
def test_issue_invitation_rejects_non_ascii_email_before_persistence(email: str) -> None:
    repository = FakeInvitationRepository()
    service = InvitationRegistrationService(repository)
    organization_id = str(uuid4())

    with pytest.raises(ValueError, match="email"):
        service.issue_invitation(
            organization_id=organization_id,
            email=email,
            actor=AuditActor.system(organization_id),
        )

    assert repository.invitation_plan is None


def test_registration_hashes_password_and_normalizes_bound_email(
    password_service: Argon2idPasswordService,
) -> None:
    repository = FakeInvitationRepository()
    service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )
    token = "opaque-invitation-token"

    result = service.register_invited_user(
        token=token,
        email=" Invitee@Example.com ",
        display_name=" Invited Analyst ",
        password="correct horse battery staple",
        request_id="request-2",
        now=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
    )

    plan = repository.registration_plan
    assert plan is not None
    assert plan.token_hash == hash_opaque_token(token)
    assert plan.email_normalized == "invitee@example.com"
    assert plan.display_name == "Invited Analyst"
    assert plan.password_algorithm == "argon2id"
    assert plan.activation_mode == INVITATION_ACTIVATE_IDENTITY
    assert plan.expected_password_hash is None
    assert password_service.verify_password(
        plan.password_hash,
        "correct horse battery staple",
    )
    assert result.display_name == "Invited Analyst"
    assert token not in repr(plan)


@pytest.mark.parametrize("token", ["", None, "x" * 513])
def test_wrong_expired_and_replayed_tokens_share_one_stable_error(
    token: object,
    password_service: Argon2idPasswordService,
) -> None:
    repository = FakeInvitationRepository()
    repository.challenge = None
    service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )

    with pytest.raises(InvalidInvitationError) as captured:
        service.register_invited_user(
            token=token,  # type: ignore[arg-type]
            email="wrong@example.com",
            display_name="Invited Analyst",
            password="correct horse battery staple",
        )

    assert captured.value.error_code == "invitation_unavailable"
    assert str(captured.value) == "邀请无效或已过期，请联系管理员重新邀请"


def test_registration_applies_existing_password_policy(
    password_service: Argon2idPasswordService,
) -> None:
    service = InvitationRegistrationService(
        FakeInvitationRepository(),
        password_service=password_service,
    )

    with pytest.raises(PasswordPolicyError):
        service.register_invited_user(
            token="token",
            email="invitee@example.com",
            display_name="Invitee",
            password="too-short",
        )


@pytest.mark.parametrize(
    "email",
    ["ü@example.com", "josé@example.com", "K@example.com", "ſ@example.com", "ß@example.com"],
)
def test_registration_rejects_non_ascii_email_as_invalid_invitation(
    password_service: Argon2idPasswordService,
    email: str,
) -> None:
    repository = FakeInvitationRepository()
    repository.challenge_email_normalized = "jose@example.com"
    service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )

    with pytest.raises(InvalidInvitationError):
        service.register_invited_user(
            token="valid-ascii-identity-token",
            email=email,
            display_name="Invitee",
            password="correct horse battery staple",
        )

    assert repository.registration_plan is None


@pytest.mark.parametrize("challenge_exists", [False, True])
def test_short_password_cannot_oracle_invitation_validity(
    password_service: Argon2idPasswordService,
    challenge_exists: bool,
) -> None:
    repository = FakeInvitationRepository()
    if not challenge_exists:
        repository.challenge = None
    service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )

    with pytest.raises(PasswordPolicyError) as captured:
        service.register_invited_user(
            token="possibly-valid-token",
            email="invitee@example.com",
            display_name="Invitee",
            password="too-short",
        )

    assert str(captured.value) == "密码至少需要 12 个字符"
    assert repository.registration_plan is None


def test_existing_identity_password_is_verified_without_replacement(
    password_service: Argon2idPasswordService,
) -> None:
    existing_hash = password_service.hash_password(
        "existing account password"
    )
    repository = FakeInvitationRepository()
    repository.challenge = InvitationRegistrationChallenge(
        activation_mode=INVITATION_ACTIVATE_MEMBERSHIP,
        password_hash=existing_hash,
        password_algorithm="argon2id",
        identity_version=7,
        user_version=5,
        token_version=3,
        membership_version=2,
    )
    service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )

    with pytest.raises(InvalidInvitationError):
        service.register_invited_user(
            token="cross-organization-token",
            email="invitee@example.com",
            display_name="Attacker supplied name",
            password="wrong existing password",
        )
    assert repository.registration_plan is None

    service.register_invited_user(
        token="cross-organization-token",
        email="invitee@example.com",
        display_name="Ignored replacement name",
        password="existing account password",
    )

    plan = repository.registration_plan
    assert plan is not None
    assert plan.activation_mode == INVITATION_ACTIVATE_MEMBERSHIP
    assert plan.password_hash is None
    assert plan.password_algorithm is None
    assert plan.expected_password_hash == existing_hash
    assert plan.expected_identity_version == 7
    assert "existing account password" not in repr(plan)
    assert existing_hash not in repr(plan)


@pytest.fixture
def sqlite_invitation_stack(
    password_service: Argon2idPasswordService,
) -> tuple[InvitationRegistrationService, object, tuple[str, str, str]]:
    engine = create_engine(
        "sqlite+pysqlite://",
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.create_function(
            "UTC_TIMESTAMP",
            1,
            lambda _precision: "2026-01-01 00:00:00.000000",
        )

    audit_id_type = audit_events.c.id.type
    audit_events.c.id.type = Integer()
    try:
        identity_metadata.create_all(engine)
    finally:
        audit_events.c.id.type = audit_id_type

    organization_ids = tuple(str(uuid4()) for _ in range(3))
    with engine.begin() as connection:
        connection.execute(
            organizations.insert(),
            [
                {
                    "id": organization_id,
                    "name": f"Organization {index}",
                    "slug": f"organization-{index}-{organization_id}",
                    "status": "active",
                }
                for index, organization_id in enumerate(organization_ids, start=1)
            ],
        )
    service = InvitationRegistrationService(
        IdentityRepository(settings=Settings(_env_file=None), engine=engine),
        password_service=password_service,
    )
    return service, engine, organization_ids


def test_cross_organization_invitation_acceptance_preserves_global_identity(
    sqlite_invitation_stack,
    password_service: Argon2idPasswordService,
) -> None:
    service, engine, (organization_1, organization_2, organization_3) = (
        sqlite_invitation_stack
    )
    email = "shared.identity@example.invalid"
    issued_1 = service.issue_invitation(
        organization_id=organization_1,
        email=email,
        actor=AuditActor.system(organization_1),
        now=datetime(2026, 8, 24, 8, 0, tzinfo=UTC),
    )
    issued_2 = service.issue_invitation(
        organization_id=organization_2,
        email=email.upper(),
        actor=AuditActor.system(organization_2),
        now=datetime(2026, 8, 24, 8, 1, tzinfo=UTC),
    )

    assert issued_2.user_id == issued_1.user_id
    assert issued_2.membership_id != issued_1.membership_id
    first = service.register_invited_user(
        token=issued_1.token,
        email=email,
        display_name="Original Global Name",
        password="existing account password",
        now=datetime(2026, 8, 24, 8, 2, tzinfo=UTC),
    )
    assert first.organization_id == organization_1

    with engine.connect() as connection:
        activated_user = connection.execute(
            select(users).where(users.c.id == issued_1.user_id)
        ).mappings().one()
        rebased_token = connection.execute(
            select(one_time_tokens).where(
                one_time_tokens.c.token_hash == hash_opaque_token(issued_2.token)
            )
        ).mappings().one()
    original_hash = str(activated_user["password_hash"])
    original_user_version = int(activated_user["version"])
    assert activated_user["status"] == "active"
    assert activated_user["auth_version"] == 2
    assert activated_user["display_name"] == "Original Global Name"
    assert password_service.verify_password(
        original_hash,
        "existing account password",
    )
    assert rebased_token["identity_version"] == 2
    assert rebased_token["consumed_at"] is None

    with pytest.raises(InvalidInvitationError):
        service.register_invited_user(
            token=issued_2.token,
            email=email,
            display_name="Unauthorized Replacement",
            password="wrong existing password",
            now=datetime(2026, 8, 24, 8, 3, tzinfo=UTC),
        )

    second = service.register_invited_user(
        token=issued_2.token,
        email=email,
        display_name="Ignored Replacement Name",
        password="existing account password",
        now=datetime(2026, 8, 24, 8, 4, tzinfo=UTC),
    )
    assert second.organization_id == organization_2
    assert second.display_name == "Original Global Name"

    with engine.connect() as connection:
        preserved_user = connection.execute(
            select(users).where(users.c.id == issued_1.user_id)
        ).mappings().one()
        memberships = connection.execute(
            select(
                organization_memberships.c.organization_id,
                organization_memberships.c.status,
            ).where(organization_memberships.c.user_id == issued_1.user_id)
        ).all()
    assert preserved_user["password_hash"] == original_hash
    assert preserved_user["display_name"] == "Original Global Name"
    assert preserved_user["auth_version"] == 2
    assert preserved_user["version"] == original_user_version
    assert set(memberships) == {
        (organization_1, "active"),
        (organization_2, "active"),
    }

    with pytest.raises(InvalidInvitationError):
        service.register_invited_user(
            token=issued_2.token,
            email=email,
            display_name="Replay",
            password="existing account password",
            now=datetime(2026, 8, 24, 8, 5, tzinfo=UTC),
        )
    with pytest.raises(InvitationCreationError):
        service.issue_invitation(
            organization_id=organization_2,
            email=email,
            actor=AuditActor.system(organization_2),
            now=datetime(2026, 8, 24, 8, 6, tzinfo=UTC),
        )

    issued_3 = service.issue_invitation(
        organization_id=organization_3,
        email=email,
        actor=AuditActor.system(organization_3),
        now=datetime(2026, 8, 24, 8, 7, tzinfo=UTC),
    )
    assert issued_3.user_id == issued_1.user_id
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(users).where(
                users.c.email_normalized == email
            )
        ).scalar_one() == 1
        assert connection.execute(
            select(organization_memberships.c.status).where(
                organization_memberships.c.id == issued_3.membership_id
            )
        ).scalar_one() == "invited"


class _Result:
    def __init__(
        self,
        *,
        scalar: object | None = None,
        mapping: dict[str, object] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._scalar = scalar
        self._mapping = mapping
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._mapping


class _CapturingConnection:
    def __init__(
        self,
        registration_row: dict[str, object] | None = None,
        existing_invited_user: dict[str, object] | None = None,
        existing_invited_membership: dict[str, object] | None = None,
    ) -> None:
        self.registration_row = registration_row
        self.existing_invited_user = existing_invited_user
        self.existing_invited_membership = existing_invited_membership
        self.statements: list[Any] = []

    def execute(self, statement: Any, *_args: object, **_kwargs: object) -> _Result:
        self.statements.append(statement)
        if statement.is_select:
            selected_names = {column.key for column in statement.selected_columns}
            if "token_id" in selected_names:
                return _Result(mapping=self.registration_row)
            if "password_algorithm" in selected_names:
                return _Result(mapping=self.existing_invited_user)
            if selected_names == {"id", "status"}:
                return _Result(mapping=self.existing_invited_membership)
            return _Result(scalar="organization-1")
        return _Result(rowcount=1)


class _Transaction:
    def __init__(self, connection: _CapturingConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _CapturingConnection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


class _CapturingEngine:
    def __init__(
        self,
        registration_row: dict[str, object] | None = None,
        existing_invited_user: dict[str, object] | None = None,
        existing_invited_membership: dict[str, object] | None = None,
    ) -> None:
        self.connection = _CapturingConnection(
            registration_row,
            existing_invited_user,
            existing_invited_membership,
        )
        self.begin_count = 0

    def begin(self) -> _Transaction:
        self.begin_count += 1
        return _Transaction(self.connection)

    def connect(self) -> _Transaction:
        return _Transaction(self.connection)


class _FailingBeginEngine:
    def __init__(self, mysql_error_code: int) -> None:
        self.mysql_error_code = mysql_error_code

    def begin(self):
        raise OperationalError(
            None,
            None,
            Exception(self.mysql_error_code, "synthetic lock failure"),
        )


def _invitation_plan() -> UserInvitationPlan:
    return UserInvitationPlan(
        token_id="token-record-1",
        user_id="user-1",
        email="invitee@example.com",
        email_normalized="invitee@example.com",
        placeholder_display_name="Invited user",
        organization_id="organization-1",
        membership_id="membership-1",
        identity_version=1,
        token_hash=b"t" * 32,
        expires_at=datetime(2026, 8, 25, 8, 0),
        created_at=datetime(2026, 8, 24, 8, 0),
        request_id="request-1",
        actor_kind="system",
        actor_membership_id=None,
        actor_user_id=None,
    )


def _registration_plan(
    *,
    activation_mode: str = INVITATION_ACTIVATE_IDENTITY,
    expected_password_hash: str | None = None,
    expected_password_algorithm: str | None = None,
) -> InvitationRegistrationPlan:
    return InvitationRegistrationPlan(
        token_hash=b"t" * 32,
        email_normalized="invitee@example.com",
        display_name="Invitee",
        activation_mode=activation_mode,
        password_hash=(
            "opaque-password-hash"
            if activation_mode == INVITATION_ACTIVATE_IDENTITY
            else None
        ),
        password_algorithm=(
            "argon2id"
            if activation_mode == INVITATION_ACTIVATE_IDENTITY
            else None
        ),
        expected_password_hash=expected_password_hash,
        expected_password_algorithm=expected_password_algorithm,
        expected_identity_version=1,
        expected_user_version=1,
        expected_token_version=1,
        expected_membership_version=1,
        completed_at=datetime(2026, 8, 24, 9, 0),
        request_id="request-2",
    )


def _registration_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "token_id": "token-record-1",
        "organization_id": "organization-1",
        "membership_id": "membership-1",
        "user_id": "user-1",
        "token_identity_version": 1,
        "expires_at": datetime(2026, 8, 25, 8, 0),
        "consumed_at": None,
        "token_version": 1,
        "email": "invitee@example.com",
        "email_normalized": "invitee@example.com",
        "display_name": "Invited user",
        "password_hash": None,
        "password_algorithm": None,
        "user_status": "invited",
        "auth_version": 1,
        "user_version": 1,
        "user_deleted_at": None,
        "membership_status": "invited",
        "membership_version": 1,
        "organization_status": "active",
        "organization_deleted_at": None,
    }
    row.update(overrides)
    return row


def test_repository_creates_invitation_and_audit_in_one_transaction() -> None:
    engine = _CapturingEngine()
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    repository.create_user_invitation(_invitation_plan())

    assert engine.begin_count == 1
    inserted_tables = [
        statement.table
        for statement in engine.connection.statements
        if statement.is_insert
    ]
    assert inserted_tables == [users, organization_memberships, one_time_tokens, audit_events]
    token_insert = next(
        statement
        for statement in engine.connection.statements
        if getattr(statement, "table", None) is one_time_tokens
    )
    token_parameters = token_insert.compile().params
    assert token_parameters["token_hash"] == b"t" * 32
    assert token_parameters["purpose"] == "user_invitation"
    assert "token" not in token_parameters


def test_repository_reissues_invited_user_and_invalidates_previous_token() -> None:
    engine = _CapturingEngine(
        existing_invited_user={
            "id": "existing-user",
            "status": "invited",
            "password_hash": None,
            "password_algorithm": None,
            "auth_version": 3,
            "deleted_at": None,
        },
        existing_invited_membership={
            "id": "existing-membership",
            "status": "invited",
        },
    )
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    persisted = repository.create_user_invitation(_invitation_plan())

    assert persisted.user_id == "existing-user"
    assert persisted.membership_id == "existing-membership"
    inserts = [statement for statement in engine.connection.statements if statement.is_insert]
    assert [statement.table for statement in inserts] == [one_time_tokens, audit_events]
    invalidation = next(
        statement for statement in engine.connection.statements if statement.is_update
    )
    assert invalidation.table is one_time_tokens
    token_insert = inserts[0].compile().params
    assert token_insert["user_id"] == "existing-user"
    assert token_insert["membership_id"] == "existing-membership"
    assert token_insert["identity_version"] == 3
    assert inserts[1].compile().params["resource_id"] == "existing-membership"


def test_repository_adds_cross_organization_membership_for_active_identity() -> None:
    engine = _CapturingEngine(
        existing_invited_user={
            "id": "existing-user",
            "status": "active",
            "password_hash": "opaque",
            "password_algorithm": "argon2id",
            "auth_version": 2,
            "deleted_at": None,
        }
    )
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    persisted = repository.create_user_invitation(_invitation_plan())

    assert persisted.user_id == "existing-user"
    assert persisted.membership_id == "membership-1"
    inserts = [
        statement.table
        for statement in engine.connection.statements
        if statement.is_insert
    ]
    assert inserts == [organization_memberships, one_time_tokens, audit_events]


def test_repository_adds_cross_organization_membership_for_invited_identity() -> None:
    engine = _CapturingEngine(
        existing_invited_user={
            "id": "existing-user",
            "status": "invited",
            "password_hash": None,
            "password_algorithm": None,
            "auth_version": 1,
            "deleted_at": None,
        },
    )
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    persisted = repository.create_user_invitation(_invitation_plan())

    assert persisted.user_id == "existing-user"
    assert persisted.membership_id == "membership-1"
    assert [
        statement.table
        for statement in engine.connection.statements
        if statement.is_insert
    ] == [organization_memberships, one_time_tokens, audit_events]


@pytest.mark.parametrize("membership_status", ["active", "suspended", "removed"])
def test_repository_rejects_existing_non_invited_membership_in_same_organization(
    membership_status: str,
) -> None:
    engine = _CapturingEngine(
        existing_invited_user={
            "id": "existing-user",
            "status": "active",
            "password_hash": "opaque",
            "password_algorithm": "argon2id",
            "auth_version": 2,
            "deleted_at": None,
        },
        existing_invited_membership={
            "id": "existing-membership",
            "status": membership_status,
        },
    )
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    with pytest.raises(InvitationCreationError):
        repository.create_user_invitation(_invitation_plan())

    assert not any(statement.is_insert for statement in engine.connection.statements)


def test_registration_challenge_is_token_email_bound_and_repr_hides_verifier() -> None:
    verifier = "$argon2id$internal-verifier"
    engine = _CapturingEngine(
        _registration_row(
            email="Jose@example.com",
            email_normalized="jose@example.com",
            password_hash=verifier,
            password_algorithm="argon2id",
            user_status="active",
        )
    )
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    challenge = repository.find_invitation_registration_challenge(
        token_hash=b"t" * 32,
        email_normalized="jose@example.com",
        now=datetime(2026, 8, 24, 9, 0),
    )

    assert challenge is not None
    assert challenge.activation_mode == INVITATION_ACTIVATE_MEMBERSHIP
    assert challenge.password_hash == verifier
    assert verifier not in repr(challenge)
    assert repository.find_invitation_registration_challenge(
        token_hash=b"t" * 32,
        email_normalized="wrong@example.com",
        now=datetime(2026, 8, 24, 9, 0),
    ) is None


def test_repository_consumes_and_audits_registration_in_one_transaction() -> None:
    engine = _CapturingEngine(_registration_row())
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    result = repository.consume_user_invitation(_registration_plan())

    assert engine.begin_count == 1
    assert result.membership_id == "membership-1"
    updates = [statement for statement in engine.connection.statements if statement.is_update]
    assert [statement.table for statement in updates] == [
        one_time_tokens,
        users,
        organization_memberships,
        one_time_tokens,
    ]
    audit_insert = engine.connection.statements[-1]
    assert audit_insert.table is audit_events
    assert audit_insert.compile().params["action"] == "identity.registration.complete"
    assert audit_insert.compile().params["actor_kind"] == "system"


def test_repository_accepts_existing_identity_without_mutating_global_profile() -> None:
    existing_hash = "$argon2id$opaque-existing-verifier"
    engine = _CapturingEngine(
        _registration_row(
            display_name="Existing Global Name",
            password_hash=existing_hash,
            password_algorithm="argon2id",
            user_status="active",
        )
    )
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )
    plan = _registration_plan(
        activation_mode=INVITATION_ACTIVATE_MEMBERSHIP,
        expected_password_hash=existing_hash,
        expected_password_algorithm="argon2id",
    )

    result = repository.consume_user_invitation(plan)

    updates = [statement for statement in engine.connection.statements if statement.is_update]
    assert [statement.table for statement in updates] == [
        one_time_tokens,
        organization_memberships,
    ]
    assert result.display_name == "Existing Global Name"
    assert plan.display_name == "Invitee"


@pytest.mark.parametrize("mysql_error_code", [1205, 1213])
def test_acceptance_lock_victim_maps_to_stable_invalid_invitation(
    mysql_error_code: int,
) -> None:
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=_FailingBeginEngine(mysql_error_code),  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidInvitationError):
        repository.consume_user_invitation(_registration_plan())


@pytest.mark.parametrize(
    "overrides",
    [
        {"consumed_at": datetime(2026, 8, 24, 8, 30)},
        {"expires_at": datetime(2026, 8, 24, 8, 59)},
        {"email_normalized": "other@example.com"},
        {"token_identity_version": 2},
        {"token_version": 2},
        {"user_version": 2},
        {"membership_version": 2},
        {"membership_status": "removed"},
    ],
)
def test_repository_rejects_all_invalid_invitation_contexts_before_updates(
    overrides: dict[str, object],
) -> None:
    engine = _CapturingEngine(_registration_row(**overrides))
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidInvitationError):
        repository.consume_user_invitation(_registration_plan())

    assert len(engine.connection.statements) == 3
    assert all(statement.is_select for statement in engine.connection.statements)


def test_one_time_token_schema_has_complete_tenant_and_identity_binding() -> None:
    assert one_time_tokens.c.organization_id.nullable is False
    assert one_time_tokens.c.membership_id.nullable is False
    assert one_time_tokens.c.identity_version.nullable is False
    composite_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in one_time_tokens.foreign_key_constraints
    }
    assert (
        "organization_memberships.id",
        "organization_memberships.user_id",
        "organization_memberships.organization_id",
    ) in composite_targets
    check_sql = {str(constraint.sqltext) for constraint in one_time_tokens.constraints if hasattr(constraint, "sqltext")}
    assert "purpose IN ('user_invitation', 'password_reset')" in check_sql
    assert "identity_version >= 1" in check_sql
