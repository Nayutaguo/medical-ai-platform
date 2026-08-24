import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "migrations" / "versions"


def _load_revision(filename: str):
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_control_plane_revision_has_explicit_baseline_and_rollback() -> None:
    baseline = _load_revision("001_existing_analytics_baseline.py")
    identity = _load_revision("002_identity_control_plane.py")
    facility_scopes = _load_revision("003_facility_scopes.py")
    facility_index = _load_revision("004_inpatient_facility_scope_index.py")
    security = _load_revision("005_control_plane_security.py")

    assert baseline.revision == "001_existing_analytics_baseline"
    assert baseline.down_revision is None
    assert identity.revision == "002_identity_control_plane"
    assert identity.down_revision == baseline.revision
    assert facility_scopes.revision == "003_facility_scopes"
    assert facility_scopes.down_revision == identity.revision
    assert facility_index.revision == "004_facility_scope_index"
    assert len(facility_index.revision) <= 32
    assert facility_index.down_revision == facility_scopes.revision
    assert facility_index.INDEX_NAME == "idx_inpatient_facility_year"
    assert facility_index.TABLE_NAME == "inpatient"
    assert security.revision == "005_control_plane_security"
    assert len(security.revision) <= 32
    assert security.down_revision == facility_index.revision
    assert (identity.SQL_MIGRATIONS / "002_identity_control_plane.up.sql").is_file()
    assert (identity.SQL_MIGRATIONS / "002_identity_control_plane.down.sql").is_file()

    up_sql = (identity.SQL_MIGRATIONS / "002_identity_control_plane.up.sql").read_text(
        encoding="utf-8"
    )
    executable_sql = "\n".join(
        line for line in up_sql.splitlines() if not line.lstrip().startswith("--")
    )
    statements = identity._split_sql_statements(executable_sql)
    assert len(statements) == 12
    assert "Password verifier; never plaintext" in statements[0]

    assert (facility_scopes.SQL_MIGRATIONS / "003_facility_scopes.up.sql").is_file()
    assert (facility_scopes.SQL_MIGRATIONS / "003_facility_scopes.down.sql").is_file()
    scope_up_sql = (
        facility_scopes.SQL_MIGRATIONS / "003_facility_scopes.up.sql"
    ).read_text(encoding="utf-8")
    executable_scope_sql = "\n".join(
        line for line in scope_up_sql.splitlines() if not line.lstrip().startswith("--")
    )
    scope_statements = facility_scopes._split_sql_statements(executable_scope_sql)
    assert len(scope_statements) == 3
    assert "CREATE TABLE IF NOT EXISTS facilities" in scope_statements[0]
    assert "CREATE TABLE IF NOT EXISTS organization_facilities" in scope_statements[1]
    assert "CREATE TABLE IF NOT EXISTS membership_facility_scopes" in scope_statements[2]
    assert "ON UPDATE" not in executable_scope_sql.upper()

    assert (security.SQL_MIGRATIONS / "005_control_plane_security.up.sql").is_file()
    assert (security.SQL_MIGRATIONS / "005_control_plane_security.down.sql").is_file()
    security_up_sql = (
        security.SQL_MIGRATIONS / "005_control_plane_security.up.sql"
    ).read_text(encoding="utf-8")
    executable_security_sql = "\n".join(
        line
        for line in security_up_sql.splitlines()
        if not line.lstrip().startswith("--")
    )
    security_statements = security._split_sql_statements(executable_security_sql)
    assert len(security_statements) == 8
    assert len(security.LEGACY_COLUMN_COMMENTS) == 87
    assert "fk_dataset_versions_import_job_org" in security_statements[-1]


def test_fresh_offline_upgrade_and_security_downgrade_generate_mysql_sql() -> None:
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "004_facility_scope_index -> 005_control_plane_security" in upgrade.stdout
    assert "fk_audit_events_actor_membership_context" in upgrade.stdout
    assert "fk_dataset_versions_import_job_org" in upgrade.stdout
    assert "uq_organization_facilities_facility" in upgrade.stdout
    assert "Creation time in UTC." in upgrade.stdout
    assert "DEFAULT (UTC_TIMESTAMP(6))" in upgrade.stdout
    assert "DEFAULT UTC_TIMESTAMP(6)" not in upgrade.stdout

    downgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade",
            "005_control_plane_security:004_facility_scope_index",
            "--sql",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "005_control_plane_security -> 004_facility_scope_index" in downgrade.stdout
    assert "DROP COLUMN actor_membership_id" in downgrade.stdout
    assert "fk_dataset_versions_import_job" in downgrade.stdout
    assert "Explicit grants; no rows means no facility access" in downgrade.stdout


def test_alembic_configuration_contains_no_database_credentials() -> None:
    configuration = (ROOT / "alembic.ini").read_text(encoding="utf-8")
    environment = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "MYSQL_PASSWORD" not in configuration
    assert "change_me" not in configuration
    assert "invalid-runtime-placeholder" in configuration
    assert "hide_password=False" not in environment
    assert "set_main_option" not in environment
