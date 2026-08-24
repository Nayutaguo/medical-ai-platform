from datetime import datetime

import pytest

from medical_ai.authorization import AccessContext
from medical_ai.identity.models import (
    ActiveSession,
    BootstrapPlan,
    NewSession,
    UserCredential,
)
from medical_ai.identity.passwords import (
    Argon2idPasswordService,
    PasswordPolicy,
    PasswordPolicyError,
)
from medical_ai.identity.tokens import generate_opaque_token, hash_opaque_token, verify_opaque_token


@pytest.fixture
def password_service() -> Argon2idPasswordService:
    # Lower test-only costs keep the suite fast; production defaults remain
    # explicit and substantially stronger.
    return Argon2idPasswordService(time_cost=1, memory_cost_kib=8_192, parallelism=1)


def test_passwords_are_stored_as_salted_argon2id_verifiers(
    password_service: Argon2idPasswordService,
) -> None:
    first = password_service.hash_password("correct horse battery staple")
    second = password_service.hash_password("correct horse battery staple")

    assert first.startswith("$argon2id$")
    assert first != second
    assert password_service.verify_password(first, "correct horse battery staple") is True
    assert password_service.verify_password(first, "wrong password") is False


def test_malformed_or_non_argon2_hash_is_rejected_without_raising(
    password_service: Argon2idPasswordService,
) -> None:
    assert password_service.verify_password("not-a-hash", "anything") is False
    assert password_service.needs_rehash("not-a-hash") is True


def test_password_policy_allows_passphrases_but_bounds_inputs() -> None:
    policy = PasswordPolicy(minimum_characters=12, maximum_bytes=32)

    policy.validate("a sufficiently long phrase")
    with pytest.raises(PasswordPolicyError, match="至少需要"):
        policy.validate("too short")
    with pytest.raises(PasswordPolicyError, match="最大长度"):
        policy.validate("界" * 12)


def test_opaque_tokens_are_unique_hash_only_values() -> None:
    first = generate_opaque_token()
    second = generate_opaque_token()

    assert first.value != second.value
    assert first.digest != second.digest
    assert len(first.digest) == 32
    assert first.digest == hash_opaque_token(first.value)
    assert first.value.encode() not in first.digest
    assert verify_opaque_token(first.value, first.digest) is True
    assert verify_opaque_token(second.value, first.digest) is False
    assert "value" not in repr(first)


def test_identity_models_never_repr_verifiers_or_token_digests() -> None:
    password_hash = "$argon2id$must-not-enter-logs"
    session_digest = b"sensitive-session-digest-value"
    csrf_digest = b"sensitive-csrf-digest-value-123"
    user = UserCredential(
        user_id="user-1",
        email="user@example.com",
        display_name="User",
        status="active",
        password_hash=password_hash,
        password_algorithm="argon2id",
        auth_version=1,
    )
    new_session = NewSession(
        session_id="session-1",
        user_id="user-1",
        membership_id="membership-1",
        organization_id="organization-1",
        session_token_hash=session_digest,
        csrf_token_hash=csrf_digest,
        expires_at=datetime(2026, 8, 24, 12, 0),
        idle_expires_at=datetime(2026, 8, 24, 10, 30),
        identity_version=1,
        authorization_version=1,
    )
    bootstrap = BootstrapPlan(
        user_id="user-1",
        email="user@example.com",
        email_normalized="user@example.com",
        display_name="User",
        password_hash=password_hash,
        organization_id="organization-1",
        organization_name="Hospital A",
        organization_slug="hospital-a",
        membership_id="membership-1",
        permission_ids=(),
        roles=(),
    )
    active = ActiveSession(
        session_id="session-1",
        user_id="user-1",
        email="user@example.com",
        display_name="User",
        organization_name="Hospital A",
        expires_at=datetime(2026, 8, 24, 12, 0),
        idle_expires_at=datetime(2026, 8, 24, 10, 30),
        csrf_token_hash=csrf_digest,
        access_context=AccessContext(
            user_id="user-1",
            organization_id="organization-1",
            membership_id="membership-1",
            permissions=frozenset(),
            allowed_facility_ids=frozenset(),
            identity_version=1,
            authorization_version=1,
        ),
    )

    rendered = " ".join(map(repr, (user, new_session, bootstrap, active)))
    assert password_hash not in rendered
    assert repr(session_digest) not in rendered
    assert repr(csrf_digest) not in rendered
