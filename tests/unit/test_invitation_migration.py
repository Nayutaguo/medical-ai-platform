from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
REVISION_PATH = ROOT / "migrations" / "versions" / "006_invitation_registration.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("invitation_revision", REVISION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invitation_revision_follows_security_hardening_and_has_rollback() -> None:
    revision = _load_revision()

    assert revision.revision == "006_invitation_registration"
    assert len(revision.revision) <= 32
    assert revision.down_revision == "005_control_plane_security"
    assert callable(revision.upgrade)
    assert callable(revision.downgrade)


def test_invitation_upgrade_and_downgrade_generate_explicit_mysql_sql() -> None:
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "005_control_plane_security -> 006_invitation_registration" in upgrade.stdout
    assert "fk_one_time_tokens_membership_context" in upgrade.stdout
    assert "ck_one_time_tokens_invitation_purpose" in upgrade.stdout
    assert "identity_version" in upgrade.stdout
    assert "DELETE FROM one_time_tokens" not in upgrade.stdout

    downgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade",
            "006_invitation_registration:005_control_plane_security",
            "--sql",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "006_invitation_registration -> 005_control_plane_security" in downgrade.stdout
    assert "fk_one_time_tokens_user" in downgrade.stdout
    assert "DROP COLUMN organization_id" in downgrade.stdout


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _CapturingConnection:
    def __init__(self, value: int) -> None:
        self.value = value
        self.sql = ""

    def execute(self, statement):
        self.sql = str(statement)
        return _ScalarResult(self.value)


@pytest.mark.parametrize(
    ("columns", "expected_predicate"),
    [
        ({}, None),
        (
            {
                "organization_id": {"nullable": True},
                "membership_id": {"nullable": True},
                "identity_version": {"nullable": True},
            },
            "organization_id IS NULL",
        ),
    ],
)
def test_partial_retry_preflight_rejects_legacy_or_null_bound_rows(
    monkeypatch,
    columns,
    expected_predicate,
) -> None:
    revision = _load_revision()
    connection = _CapturingConnection(1)
    monkeypatch.setattr(revision.op, "get_bind", lambda: connection)

    with pytest.raises(RuntimeError, match="006 preflight failed"):
        revision._validate_no_unbound_tokens(columns)

    if expected_predicate is None:
        assert " WHERE " not in connection.sql
    else:
        assert expected_predicate in connection.sql


def test_partial_retry_preflight_allows_fully_bound_rows(monkeypatch) -> None:
    revision = _load_revision()
    connection = _CapturingConnection(0)
    monkeypatch.setattr(revision.op, "get_bind", lambda: connection)
    columns = {
        "organization_id": {"nullable": False},
        "membership_id": {"nullable": False},
        "identity_version": {"nullable": False},
    }

    revision._validate_no_unbound_tokens(columns)

    assert "identity_version IS NULL" in connection.sql
