import pytest

from medical_ai.authorization import PermissionCode
from medical_ai.config import Settings
from medical_ai.identity import audit_events
from medical_ai.identity.errors import BootstrapAlreadyCompletedError
from medical_ai.identity.models import BootstrapPlan, BootstrapResult
from medical_ai.identity.passwords import Argon2idPasswordService, PasswordPolicyError
from medical_ai.repositories import IdentityRepository
from medical_ai.services import IdentityAdministrationService


class FakeBootstrapRepository:
    def __init__(self) -> None:
        self.plan: BootstrapPlan | None = None
        self.already_bootstrapped = False

    def bootstrap_first_administrator(self, plan: BootstrapPlan) -> BootstrapResult:
        if self.already_bootstrapped:
            raise BootstrapAlreadyCompletedError
        self.plan = plan
        return BootstrapResult(
            user_id=plan.user_id,
            organization_id=plan.organization_id,
            membership_id=plan.membership_id,
        )


@pytest.fixture
def password_service() -> Argon2idPasswordService:
    return Argon2idPasswordService(time_cost=1, memory_cost_kib=8_192, parallelism=1)


def test_bootstrap_seeds_explicit_admin_and_analyst_roles_without_implicit_data_access(
    password_service,
) -> None:
    repository = FakeBootstrapRepository()
    service = IdentityAdministrationService(repository, password_service=password_service)

    result = service.bootstrap_first_administrator(
        email=" Admin@Example.com ",
        display_name="Platform Administrator",
        password="correct horse battery staple",
        organization_name="Hospital A",
        organization_slug="hospital-a",
    )

    plan = repository.plan
    assert plan is not None
    assert result.user_id == plan.user_id
    assert plan.email_normalized == "admin@example.com"
    assert password_service.verify_password(
        plan.password_hash,
        "correct horse battery staple",
    )
    assert {permission for permission, _ in plan.permission_ids} == set(PermissionCode)
    roles = {role.role_key: role for role in plan.roles}
    assert roles["organization_admin"].assign_to_bootstrap_membership is True
    assert roles["data_analyst"].assign_to_bootstrap_membership is False
    assert PermissionCode.USERS_MANAGE in roles["organization_admin"].permissions
    assert PermissionCode.ANALYTICS_QUERY_EXECUTE not in roles["organization_admin"].permissions
    assert PermissionCode.ANALYTICS_QUERY_EXECUTE in roles["data_analyst"].permissions


def test_bootstrap_rejects_invalid_identifiers_before_persistence(password_service) -> None:
    repository = FakeBootstrapRepository()
    service = IdentityAdministrationService(repository, password_service=password_service)

    with pytest.raises(ValueError, match="email"):
        service.bootstrap_first_administrator(
            email="invalid",
            display_name="Admin",
            password="correct horse battery staple",
            organization_name="Hospital A",
            organization_slug="hospital-a",
        )
    assert repository.plan is None


def test_bootstrap_enforces_password_policy(password_service) -> None:
    service = IdentityAdministrationService(
        FakeBootstrapRepository(),
        password_service=password_service,
    )

    with pytest.raises(PasswordPolicyError):
        service.bootstrap_first_administrator(
            email="admin@example.com",
            display_name="Admin",
            password="too-short",
            organization_name="Hospital A",
            organization_slug="hospital-a",
        )


def test_repository_refusal_is_propagated(password_service) -> None:
    repository = FakeBootstrapRepository()
    repository.already_bootstrapped = True
    service = IdentityAdministrationService(repository, password_service=password_service)

    with pytest.raises(BootstrapAlreadyCompletedError):
        service.bootstrap_first_administrator(
            email="admin@example.com",
            display_name="Admin",
            password="correct horse battery staple",
            organization_name="Hospital A",
            organization_slug="hospital-a",
        )


class _EmptyScalarResult:
    def scalar_one_or_none(self):
        return None


class _CapturingBootstrapConnection:
    def __init__(self) -> None:
        self.statements = []

    def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return _EmptyScalarResult()


class _BootstrapTransaction:
    def __init__(self, connection: _CapturingBootstrapConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return None


class _CapturingBootstrapEngine:
    def __init__(self) -> None:
        self.connection = _CapturingBootstrapConnection()

    def begin(self):
        return _BootstrapTransaction(self.connection)


def test_repository_bootstrap_audit_uses_trusted_membership_context() -> None:
    engine = _CapturingBootstrapEngine()
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
        engine=engine,  # type: ignore[arg-type]
    )
    plan = BootstrapPlan(
        user_id="user-1",
        email="admin@example.com",
        email_normalized="admin@example.com",
        display_name="Administrator",
        password_hash="opaque-password-verifier",
        organization_id="organization-1",
        organization_name="Hospital A",
        organization_slug="hospital-a",
        membership_id="membership-1",
        permission_ids=(),
        roles=(),
    )

    repository.bootstrap_first_administrator(plan)

    audit_statement = next(
        statement
        for statement in engine.connection.statements
        if getattr(statement, "table", None) is audit_events
    )
    parameters = audit_statement.compile().params
    assert parameters["organization_id"] == "organization-1"
    assert parameters["actor_kind"] == "user"
    assert parameters["actor_membership_id"] == "membership-1"
    assert parameters["actor_user_id"] == "user-1"
