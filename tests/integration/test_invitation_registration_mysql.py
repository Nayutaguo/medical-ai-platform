from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from threading import Barrier
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


@requires_mysql
def test_cross_organization_acceptance_preserves_existing_identity() -> None:
    settings = get_settings()
    engine = create_engine(settings.mysql_url(), pool_pre_ping=True)
    repository = IdentityRepository(settings=settings, engine=engine)
    password_service = Argon2idPasswordService(
        time_cost=1,
        memory_cost_kib=8_192,
        parallelism=1,
    )
    service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )
    organization_ids = tuple(str(uuid4()) for _ in range(3))
    email = f"multi-org-{uuid4()}@example.invalid"
    user_id: str | None = None

    try:
        with engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                [
                    {
                        "id": organization_id,
                        "name": f"Synthetic Multi-Org Test {index}",
                        "slug": f"multi-org-test-{uuid4()}",
                        "status": "active",
                    }
                    for index, organization_id in enumerate(
                        organization_ids,
                        start=1,
                    )
                ],
            )

        first = service.issue_invitation(
            organization_id=organization_ids[0],
            email=email,
            actor=AuditActor.system(organization_ids[0]),
            now=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        )
        second = service.issue_invitation(
            organization_id=organization_ids[1],
            email=email.upper(),
            actor=AuditActor.system(organization_ids[1]),
            now=datetime(2026, 8, 24, 10, 1, tzinfo=UTC),
        )
        user_id = first.user_id
        assert second.user_id == first.user_id

        service.register_invited_user(
            token=first.token,
            email=email,
            display_name="Canonical Multi-Org User",
            password="existing account password",
            now=datetime(2026, 8, 24, 10, 2, tzinfo=UTC),
        )
        with engine.connect() as connection:
            baseline = connection.execute(
                select(users).where(users.c.id == user_id)
            ).mappings().one()
        baseline_hash = str(baseline["password_hash"])
        baseline_version = int(baseline["version"])

        with pytest.raises(InvalidInvitationError):
            service.register_invited_user(
                token=second.token,
                email=email,
                display_name="Untrusted Replacement",
                password="wrong existing password",
                now=datetime(2026, 8, 24, 10, 3, tzinfo=UTC),
            )
        accepted = service.register_invited_user(
            token=second.token,
            email=email,
            display_name="Ignored Replacement",
            password="existing account password",
            now=datetime(2026, 8, 24, 10, 4, tzinfo=UTC),
        )
        assert accepted.organization_id == organization_ids[1]
        assert accepted.display_name == "Canonical Multi-Org User"

        third = service.issue_invitation(
            organization_id=organization_ids[2],
            email=email,
            actor=AuditActor.system(organization_ids[2]),
            now=datetime(2026, 8, 24, 10, 5, tzinfo=UTC),
        )
        assert third.user_id == user_id

        with engine.connect() as connection:
            preserved = connection.execute(
                select(users).where(users.c.id == user_id)
            ).mappings().one()
            membership_rows = connection.execute(
                select(
                    organization_memberships.c.organization_id,
                    organization_memberships.c.status,
                ).where(organization_memberships.c.user_id == user_id)
            ).all()
        assert preserved["password_hash"] == baseline_hash
        assert preserved["display_name"] == "Canonical Multi-Org User"
        assert preserved["auth_version"] == 2
        assert preserved["version"] == baseline_version
        assert set(membership_rows) == {
            (organization_ids[0], "active"),
            (organization_ids[1], "active"),
            (organization_ids[2], "invited"),
        }
    finally:
        with engine.begin() as connection:
            connection.execute(
                audit_events.delete().where(
                    audit_events.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.id.in_(organization_ids)
                )
            )
            connection.execute(
                users.delete().where(users.c.email_normalized == email)
            )
        engine.dispose()


@requires_mysql
def test_concurrent_cross_organization_invites_converge_on_one_global_user() -> None:
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
    organization_ids = (str(uuid4()), str(uuid4()))
    email = f"concurrent-{uuid4()}@example.invalid"
    barrier = Barrier(2)
    user_id: str | None = None

    try:
        with engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                [
                    {
                        "id": organization_id,
                        "name": f"Concurrent Invitation Test {index}",
                        "slug": f"concurrent-invitation-{uuid4()}",
                        "status": "active",
                    }
                    for index, organization_id in enumerate(
                        organization_ids,
                        start=1,
                    )
                ],
            )

        def issue(organization_id: str):
            barrier.wait(timeout=10)
            return service.issue_invitation(
                organization_id=organization_id,
                email=email,
                actor=AuditActor.system(organization_id),
                now=datetime(2026, 8, 24, 11, 0, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            invitations = tuple(executor.map(issue, organization_ids))

        user_id = invitations[0].user_id
        assert {invitation.user_id for invitation in invitations} == {user_id}
        assert len({invitation.membership_id for invitation in invitations}) == 2
        with engine.connect() as connection:
            assert connection.execute(
                select(func.count()).select_from(users).where(
                    users.c.email_normalized == email
                )
            ).scalar_one() == 1
            assert connection.execute(
                select(func.count())
                .select_from(organization_memberships)
                .where(
                    organization_memberships.c.user_id == user_id,
                    organization_memberships.c.status == "invited",
                )
            ).scalar_one() == 2
            assert connection.execute(
                select(func.count()).select_from(one_time_tokens).where(
                    one_time_tokens.c.user_id == user_id,
                    one_time_tokens.c.consumed_at.is_(None),
                )
            ).scalar_one() == 2
    finally:
        with engine.begin() as connection:
            connection.execute(
                audit_events.delete().where(
                    audit_events.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.id.in_(organization_ids)
                )
            )
            connection.execute(
                users.delete().where(users.c.email_normalized == email)
            )
        engine.dispose()


@requires_mysql
def test_concurrent_first_acceptance_is_retryable_without_identity_overwrite() -> None:
    settings = get_settings()
    engine = create_engine(settings.mysql_url(), pool_pre_ping=True)
    repository = IdentityRepository(settings=settings, engine=engine)
    password_service = Argon2idPasswordService(
        time_cost=1,
        memory_cost_kib=8_192,
        parallelism=1,
    )
    issuing_service = InvitationRegistrationService(
        repository,
        password_service=password_service,
    )
    organization_ids = (str(uuid4()), str(uuid4()))
    email = f"concurrent-accept-{uuid4()}@example.invalid"
    user_id: str | None = None

    try:
        with engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                [
                    {
                        "id": organization_id,
                        "name": f"Concurrent Acceptance Test {index}",
                        "slug": f"concurrent-acceptance-{uuid4()}",
                        "status": "active",
                    }
                    for index, organization_id in enumerate(
                        organization_ids,
                        start=1,
                    )
                ],
            )
        invitations = tuple(
            issuing_service.issue_invitation(
                organization_id=organization_id,
                email=email,
                actor=AuditActor.system(organization_id),
                now=datetime(2026, 8, 24, 12, index, tzinfo=UTC),
            )
            for index, organization_id in enumerate(organization_ids)
        )
        user_id = invitations[0].user_id
        challenge_barrier = Barrier(2)

        class BarrierAcceptanceRepository:
            def find_invitation_registration_challenge(self, **kwargs):
                challenge = repository.find_invitation_registration_challenge(
                    **kwargs
                )
                challenge_barrier.wait(timeout=10)
                return challenge

            def consume_user_invitation(self, plan):
                return repository.consume_user_invitation(plan)

        acceptance_services = tuple(
            InvitationRegistrationService(
                BarrierAcceptanceRepository(),  # type: ignore[arg-type]
                password_service=password_service,
            )
            for _ in invitations
        )
        submitted_names = ("Concurrent Candidate One", "Concurrent Candidate Two")

        def accept(index: int):
            try:
                result = acceptance_services[index].register_invited_user(
                    token=invitations[index].token,
                    email=email,
                    display_name=submitted_names[index],
                    password="shared concurrent password",
                    now=datetime(2026, 8, 24, 12, 5, tzinfo=UTC),
                )
                return "success", result
            except InvalidInvitationError as exc:
                return "invalid", exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(accept, range(2)))

        assert [outcome[0] for outcome in outcomes].count("success") == 1
        assert [outcome[0] for outcome in outcomes].count("invalid") == 1
        winner_index = next(
            index for index, outcome in enumerate(outcomes) if outcome[0] == "success"
        )
        loser_index = 1 - winner_index
        winner_result = outcomes[winner_index][1]

        retried = issuing_service.register_invited_user(
            token=invitations[loser_index].token,
            email=email,
            display_name="Retry Must Not Replace Identity",
            password="shared concurrent password",
            now=datetime(2026, 8, 24, 12, 6, tzinfo=UTC),
        )

        assert retried.display_name == winner_result.display_name
        assert retried.display_name == submitted_names[winner_index]
        with engine.connect() as connection:
            preserved_user = connection.execute(
                select(users).where(users.c.id == user_id)
            ).mappings().one()
            membership_statuses = connection.execute(
                select(organization_memberships.c.status).where(
                    organization_memberships.c.user_id == user_id
                )
            ).scalars().all()
            consumed_count = connection.execute(
                select(func.count()).select_from(one_time_tokens).where(
                    one_time_tokens.c.user_id == user_id,
                    one_time_tokens.c.consumed_at.is_not(None),
                )
            ).scalar_one()
        assert preserved_user["display_name"] == submitted_names[winner_index]
        assert preserved_user["auth_version"] == 2
        assert preserved_user["version"] == 2
        assert password_service.verify_password(
            str(preserved_user["password_hash"]),
            "shared concurrent password",
        )
        assert sorted(membership_statuses) == ["active", "active"]
        assert consumed_count == 2
    finally:
        with engine.begin() as connection:
            connection.execute(
                audit_events.delete().where(
                    audit_events.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.id.in_(organization_ids)
                )
            )
            connection.execute(
                users.delete().where(users.c.email_normalized == email)
            )
        engine.dispose()
