from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.identity.errors import InvalidCurrentPasswordError, InvalidPasswordResetError
from medical_ai.identity.models import (
    ActiveSession,
    MembershipGrant,
    PasswordChangePlan,
    PasswordResetChallenge,
    PasswordResetPlan,
    PasswordResetTokenPlan,
    UserCredential,
)
from medical_ai.identity.passwords import Argon2idPasswordService
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services.authentication import AuthenticationService


NOW = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)


class PasswordRepository:
    def __init__(self, verifier: str) -> None:
        self.user = UserCredential(
            user_id="user-1",
            email="Analyst@Example.com",
            display_name="Analyst",
            status="active",
            password_hash=verifier,
            password_algorithm="argon2id",
            auth_version=4,
        )
        self.memberships = [
            MembershipGrant(
                membership_id="membership-2",
                organization_id="organization-2",
                organization_name="Organization 2",
                status="active",
                authorization_version=1,
                permissions=frozenset(),
            ),
            MembershipGrant(
                membership_id="membership-1",
                organization_id="organization-1",
                organization_name="Organization 1",
                status="active",
                authorization_version=1,
                permissions=frozenset(),
            ),
        ]
        self.password_change: PasswordChangePlan | None = None
        self.reset_token: PasswordResetTokenPlan | None = None
        self.reset_plan: PasswordResetPlan | None = None
        self.persist_reset = True
        self.challenge: PasswordResetChallenge | None = None

    def find_user_by_id(self, user_id: str):
        return self.user if user_id == self.user.user_id else None

    def find_user_by_normalized_email(self, normalized_email: str):
        return self.user if normalized_email == self.user.email.casefold() else None

    def list_active_memberships(self, user_id: str):
        return self.memberships if user_id == self.user.user_id else []

    def change_password(self, plan: PasswordChangePlan) -> None:
        self.password_change = plan

    def create_password_reset_token(self, plan: PasswordResetTokenPlan) -> bool:
        self.reset_token = plan
        return self.persist_reset

    def find_password_reset_challenge(self, *, token_hash: bytes, now: datetime):
        assert token_hash == hash_opaque_token("reset-secret")
        assert now == NOW.replace(tzinfo=None)
        return self.challenge

    def consume_password_reset(self, plan: PasswordResetPlan) -> None:
        self.reset_plan = plan


def _passwords() -> Argon2idPasswordService:
    return Argon2idPasswordService(time_cost=1, memory_cost_kib=8_192, parallelism=1)


def _service(repository: PasswordRepository, passwords: Argon2idPasswordService):
    return AuthenticationService(
        repository,  # type: ignore[arg-type]
        password_service=passwords,
        password_reset_lifetime=timedelta(minutes=20),
        dummy_password_hash=passwords.hash_password("dummy password for lifecycle tests"),
    )


def _session() -> ActiveSession:
    context = AccessContext(
        user_id="user-1",
        organization_id="organization-1",
        membership_id="membership-1",
        permissions=frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}),
        allowed_facility_ids=frozenset(),
        identity_version=4,
        authorization_version=1,
        session_id="session-1",
    )
    return ActiveSession(
        session_id="session-1",
        user_id="user-1",
        email="Analyst@Example.com",
        display_name="Analyst",
        organization_name="Organization 1",
        expires_at=NOW.replace(tzinfo=None) + timedelta(hours=1),
        idle_expires_at=NOW.replace(tzinfo=None) + timedelta(minutes=20),
        csrf_token_hash=b"c" * 32,
        access_context=context,
    )


def test_password_change_reuses_argon2_and_keeps_plaintext_out_of_plan() -> None:
    passwords = _passwords()
    repository = PasswordRepository(passwords.hash_password("current password value"))

    _service(repository, passwords).change_password(
        _session(),
        "current password value",
        "replacement password value",
        request_id="request-change",
        now=NOW,
    )

    plan = repository.password_change
    assert plan is not None
    assert passwords.verify_password(plan.password_hash, "replacement password value")
    assert plan.request_id == "request-change"
    assert "current password value" not in repr(plan)
    assert "replacement password value" not in repr(plan)


def test_wrong_current_password_uses_stable_error_without_persistence() -> None:
    passwords = _passwords()
    repository = PasswordRepository(passwords.hash_password("current password value"))

    with pytest.raises(InvalidCurrentPasswordError):
        _service(repository, passwords).change_password(
            _session(),
            "wrong current password",
            "replacement password value",
            request_id=None,
            now=NOW,
        )

    assert repository.password_change is None


def test_reset_request_selects_requested_membership_and_persists_only_digest() -> None:
    passwords = _passwords()
    repository = PasswordRepository(passwords.hash_password("current password value"))

    issued = _service(repository, passwords).request_password_reset(
        " analyst@example.COM ",
        organization_id="organization-2",
        request_id="request-reset",
        now=NOW,
    )

    assert issued is not None
    assert issued.organization_id == "organization-2"
    assert issued.expires_at == NOW.replace(tzinfo=None) + timedelta(minutes=20)
    plan = repository.reset_token
    assert plan is not None
    assert plan.organization_id == "organization-2"
    assert plan.token_hash == hash_opaque_token(issued.token)
    assert issued.token not in repr(plan)


def test_unknown_or_wrong_organization_reset_request_returns_no_token() -> None:
    passwords = _passwords()
    repository = PasswordRepository(passwords.hash_password("current password value"))

    assert _service(repository, passwords).request_password_reset(
        "unknown@example.com",
        request_id=None,
        now=NOW,
    ) is None
    assert _service(repository, passwords).request_password_reset(
        "analyst@example.com",
        organization_id="organization-missing",
        request_id=None,
        now=NOW,
    ) is None
    assert repository.reset_token is None


def test_reset_completion_is_bound_to_email_organization_and_identity_version() -> None:
    passwords = _passwords()
    repository = PasswordRepository(passwords.hash_password("current password value"))
    repository.challenge = PasswordResetChallenge(
        user_id="user-1",
        organization_id="organization-1",
        membership_id="membership-1",
        email_normalized="analyst@example.com",
        identity_version=4,
        user_version=7,
        token_version=2,
    )
    service = _service(repository, passwords)

    with pytest.raises(InvalidPasswordResetError):
        service.reset_password(
            "reset-secret",
            "other@example.com",
            "organization-1",
            "replacement password value",
            request_id=None,
            now=NOW,
        )
    assert repository.reset_plan is None

    service.reset_password(
        "reset-secret",
        "Analyst@Example.com",
        "organization-1",
        "replacement password value",
        request_id="request-complete",
        now=NOW,
    )
    plan = repository.reset_plan
    assert plan is not None
    assert plan.expected_identity_version == 4
    assert passwords.verify_password(plan.password_hash, "replacement password value")
    assert "reset-secret" not in repr(plan)
