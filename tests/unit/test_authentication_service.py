from datetime import UTC, datetime, timedelta

import pytest

from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.identity.errors import (
    AuthenticationRequiredError,
    CsrfValidationError,
    InvalidCredentialsError,
    OrganizationSelectionRequiredError,
)
from medical_ai.identity.models import (
    ActiveSession,
    MembershipGrant,
    NewSession,
    UserCredential,
)
from medical_ai.identity.passwords import Argon2idPasswordService
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services import AuthenticationService


NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


class FakeIdentityRepository:
    def __init__(self, user: UserCredential | None, memberships: list[MembershipGrant]) -> None:
        self.user = user
        self.memberships = memberships
        self.created_session: NewSession | None = None
        self.active_session: ActiveSession | None = None
        self.active_token_hash: bytes | None = None
        self.revoked_session_id: str | None = None
        self.touches: list[tuple[str, datetime, datetime]] = []

    def find_user_by_normalized_email(self, normalized_email: str) -> UserCredential | None:
        if self.user and normalized_email == self.user.email.casefold():
            return self.user
        return None

    def list_active_memberships(self, user_id: str) -> list[MembershipGrant]:
        return self.memberships if self.user and user_id == self.user.user_id else []

    def create_session(self, session: NewSession, *, logged_in_at: datetime) -> None:
        self.created_session = session

    def find_active_session(self, token_hash: bytes, *, now: datetime) -> ActiveSession | None:
        if token_hash == self.active_token_hash:
            return self.active_session
        return None

    def touch_session(
        self,
        session_id: str,
        *,
        seen_at: datetime,
        idle_expires_at: datetime,
    ) -> None:
        self.touches.append((session_id, seen_at, idle_expires_at))

    def revoke_session(self, session_id: str, *, revoked_at: datetime) -> None:
        self.revoked_session_id = session_id


@pytest.fixture
def password_service() -> Argon2idPasswordService:
    return Argon2idPasswordService(time_cost=1, memory_cost_kib=8_192, parallelism=1)


def _membership(organization_id: str = "org-1") -> MembershipGrant:
    return MembershipGrant(
        membership_id=f"membership-{organization_id}",
        organization_id=organization_id,
        organization_name=f"Organization {organization_id}",
        status="active",
        authorization_version=3,
        permissions=frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}),
        allowed_facility_ids=frozenset({"facility-1"}),
    )


def _user(password_hash: str) -> UserCredential:
    return UserCredential(
        user_id="user-1",
        email="Analyst@example.com",
        display_name="Analyst",
        status="active",
        password_hash=password_hash,
        password_algorithm="argon2id",
        auth_version=4,
    )


def _service(repository, password_service) -> AuthenticationService:
    dummy = password_service.hash_password("dummy password used only in tests")
    return AuthenticationService(
        repository,
        password_service=password_service,
        idle_timeout=timedelta(minutes=30),
        absolute_timeout=timedelta(hours=12),
        touch_interval=timedelta(minutes=5),
        dummy_password_hash=dummy,
    )


def test_login_persists_only_session_and_csrf_digests(password_service) -> None:
    password = "correct horse battery staple"
    repository = FakeIdentityRepository(_user(password_service.hash_password(password)), [_membership()])

    issued = _service(repository, password_service).login(
        "  analyst@EXAMPLE.com ",
        password,
        now=NOW,
    )

    stored = repository.created_session
    assert stored is not None
    assert stored.session_token_hash == hash_opaque_token(issued.session_token)
    assert stored.csrf_token_hash == hash_opaque_token(issued.csrf_token)
    assert issued.session_token.encode() not in stored.session_token_hash
    assert stored.identity_version == 4
    assert stored.authorization_version == 3
    assert stored.idle_expires_at < stored.expires_at
    assert "session_token" not in repr(issued)
    public = issued.active_session.to_public_dict()
    assert "password" not in str(public)
    assert "token" not in str(public)


@pytest.mark.parametrize(
    ("known_user", "password"),
    [(False, "any sufficiently long password"), (True, "wrong password value")],
)
def test_unknown_and_mismatched_credentials_share_one_error(
    password_service,
    known_user: bool,
    password: str,
) -> None:
    user = _user(password_service.hash_password("correct horse battery staple")) if known_user else None
    repository = FakeIdentityRepository(user, [_membership()])

    with pytest.raises(InvalidCredentialsError, match="邮箱或密码不正确"):
        _service(repository, password_service).login("analyst@example.com", password, now=NOW)

    assert repository.created_session is None


def test_multiple_memberships_require_explicit_organization(password_service) -> None:
    password = "correct horse battery staple"
    repository = FakeIdentityRepository(
        _user(password_service.hash_password(password)),
        [_membership("org-1"), _membership("org-2")],
    )
    service = _service(repository, password_service)

    with pytest.raises(OrganizationSelectionRequiredError):
        service.login("analyst@example.com", password, now=NOW)

    issued = service.login(
        "analyst@example.com",
        password,
        organization_id="org-2",
        now=NOW,
    )
    assert issued.active_session.access_context.organization_id == "org-2"


def test_session_resolution_is_hash_based_and_expired_or_unknown_is_rejected(password_service) -> None:
    repository = FakeIdentityRepository(None, [])
    service = _service(repository, password_service)

    with pytest.raises(AuthenticationRequiredError):
        service.resolve_session("missing-token", now=NOW)


def test_logout_requires_matching_csrf_before_revocation(password_service) -> None:
    repository = FakeIdentityRepository(None, [])
    service = _service(repository, password_service)
    session_token = "browser-session-token"
    csrf_token = "browser-csrf-token"
    context = AccessContext(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        permissions=frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}),
        allowed_facility_ids=frozenset({"facility-1"}),
        identity_version=1,
        authorization_version=1,
    )
    repository.active_token_hash = hash_opaque_token(session_token)
    repository.active_session = ActiveSession(
        session_id="session-1",
        user_id="user-1",
        email="analyst@example.com",
        display_name="Analyst",
        organization_name="Org",
        expires_at=NOW.replace(tzinfo=None) + timedelta(hours=12),
        idle_expires_at=NOW.replace(tzinfo=None) + timedelta(minutes=30),
        csrf_token_hash=hash_opaque_token(csrf_token),
        access_context=context,
    )

    with pytest.raises(CsrfValidationError):
        service.logout(session_token, "wrong-csrf-token", now=NOW)
    assert repository.revoked_session_id is None

    service.logout(session_token, csrf_token, now=NOW)
    assert repository.revoked_session_id == "session-1"
