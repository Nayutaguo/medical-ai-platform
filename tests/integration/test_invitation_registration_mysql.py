from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from medical_ai.audit import AuditActor
from medical_ai.config import get_settings
from medical_ai.identity import (
    audit_events,
    membership_facility_scopes,
    membership_roles,
    one_time_tokens,
    organization_memberships,
    organizations,
    users,
)
from medical_ai.identity.errors import InvalidInvitationError
from medical_ai.identity.passwords import Argon2idPasswordService
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.repositories import IdentityRepository
from medical_ai.services import InvitationRegistrationService


pytestmark = pytest.mark.integration

requires_mysql = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_TESTS") != "1",
    reason="Set RUN_MYSQL_TESTS=1 and configure MySQL to run.",
)


@requires_mysql
def test_invitation_registration_is_bound_single_use_audited_and_unprivileged() -> None:
    settings = get_settings()
    engine = create_engine(settings.mysql_url(), pool_pre_ping=True)
    repository = IdentityRepository(settings=settings, engine=engine)
    service = InvitationRegistrationService(
        repository,
        password_service=Argon2idPasswordService(
            time_cost=1,
            memory_cost_kib=8_192,
            parallelism=1,
        ),
    )
    organization_id = str(uuid4())
    email = f"invite-{uuid4()}@example.invalid"
    invitation = None

    try:
        with engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    id=organization_id,
                    name="Synthetic Invitation Test Organization",
                    slug=f"invitation-test-{uuid4()}",
                    status="active",
                )
            )

        invitation = service.issue_invitation(
            organization_id=organization_id,
            email=email,
            actor=AuditActor.system(organization_id),
            lifetime=timedelta(minutes=5),
            request_id="integration-invitation-create",
            now=datetime(2026, 8, 24, 8, 0, tzinfo=UTC),
        )
        first_token = invitation.token

        reissued = service.issue_invitation(
            organization_id=organization_id,
            email=email,
            actor=AuditActor.system(organization_id),
            lifetime=timedelta(hours=1),
            request_id="integration-invitation-reissue",
            now=datetime(2026, 8, 24, 8, 6, tzinfo=UTC),
        )
        assert reissued.user_id == invitation.user_id
        assert reissued.membership_id == invitation.membership_id
        invitation = reissued

        with engine.connect() as connection:
            invited_user = connection.execute(
                select(users).where(users.c.id == invitation.user_id)
            ).mappings().one()
            invited_membership = connection.execute(
                select(organization_memberships).where(
                    organization_memberships.c.id == invitation.membership_id
                )
            ).mappings().one()
            persisted_token = connection.execute(
                select(one_time_tokens).where(
                    one_time_tokens.c.user_id == invitation.user_id,
                    one_time_tokens.c.consumed_at.is_(None),
                )
            ).mappings().one()
            assert invited_user["status"] == "invited"
            assert invited_user["password_hash"] is None
            assert invited_membership["status"] == "invited"
            assert persisted_token["organization_id"] == organization_id
            assert persisted_token["membership_id"] == invitation.membership_id
            assert persisted_token["identity_version"] == invited_user["auth_version"]
            assert bytes(persisted_token["token_hash"]) == hash_opaque_token(invitation.token)

        with pytest.raises(InvalidInvitationError):
            service.register_invited_user(
                token=first_token,
                email=email,
                display_name="Synthetic Invitee",
                password="correct horse battery staple",
                now=datetime(2026, 8, 24, 8, 7, tzinfo=UTC),
            )

        with pytest.raises(InvalidInvitationError):
            service.register_invited_user(
                token=invitation.token,
                email="wrong@example.invalid",
                display_name="Synthetic Invitee",
                password="correct horse battery staple",
                now=datetime(2026, 8, 24, 8, 10, tzinfo=UTC),
            )

        result = service.register_invited_user(
            token=invitation.token,
            email=email.upper(),
            display_name="Synthetic Invitee",
            password="correct horse battery staple",
            request_id="integration-registration-complete",
            now=datetime(2026, 8, 24, 8, 11, tzinfo=UTC),
        )
        assert result.membership_id == invitation.membership_id

        with engine.connect() as connection:
            active_user = connection.execute(
                select(users).where(users.c.id == invitation.user_id)
            ).mappings().one()
            active_membership = connection.execute(
                select(organization_memberships).where(
                    organization_memberships.c.id == invitation.membership_id
                )
            ).mappings().one()
            consumed_at = connection.execute(
                select(one_time_tokens.c.consumed_at).where(
                    one_time_tokens.c.token_hash
                    == hash_opaque_token(invitation.token)
                )
            ).scalar_one()
            role_count = connection.execute(
                select(func.count())
                .select_from(membership_roles)
                .where(membership_roles.c.membership_id == invitation.membership_id)
            ).scalar_one()
            facility_count = connection.execute(
                select(func.count())
                .select_from(membership_facility_scopes)
                .where(
                    membership_facility_scopes.c.membership_id
                    == invitation.membership_id
                )
            ).scalar_one()
            audit_rows = connection.execute(
                select(audit_events.c.action, audit_events.c.actor_kind)
                .where(audit_events.c.organization_id == organization_id)
                .order_by(audit_events.c.id)
            ).all()

        assert active_user["status"] == "active"
        assert active_user["password_algorithm"] == "argon2id"
        assert active_user["auth_version"] == 2
        assert active_membership["status"] == "active"
        assert consumed_at is not None
        assert role_count == 0
        assert facility_count == 0
        assert audit_rows == [
            ("identity.invitation.create", "system"),
            ("identity.invitation.create", "system"),
            ("identity.registration.complete", "system"),
        ]

        with pytest.raises(InvalidInvitationError):
            service.register_invited_user(
                token=invitation.token,
                email=email,
                display_name="Synthetic Invitee",
                password="correct horse battery staple",
                now=datetime(2026, 8, 24, 8, 12, tzinfo=UTC),
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                audit_events.delete().where(
                    audit_events.c.organization_id == organization_id
                )
            )
            connection.execute(
                organizations.delete().where(organizations.c.id == organization_id)
            )
            if invitation is not None:
                connection.execute(users.delete().where(users.c.id == invitation.user_id))
        engine.dispose()
