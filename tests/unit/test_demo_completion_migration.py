from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint
from sqlalchemy.dialects import mysql

from medical_ai.identity.schema import analysis_history


ROOT = Path(__file__).resolve().parents[2]
REVISION_PATH = ROOT / "migrations" / "versions" / "007_demo_completion.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("demo_completion_revision", REVISION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_completion_revision_follows_invitation_registration() -> None:
    revision = _load_revision()

    assert revision.revision == "007_demo_completion"
    assert len(revision.revision) <= 32
    assert revision.down_revision == "006_invitation_registration"
    assert callable(revision.upgrade)
    assert callable(revision.downgrade)


def test_history_create_table_matches_identity_metadata(monkeypatch) -> None:
    revision = _load_revision()
    captured: dict[str, object] = {}

    def capture_create_table(table_name, *items, **options):
        captured.update(table_name=table_name, items=items, options=options)

    monkeypatch.setattr(revision.op, "create_table", capture_create_table)
    revision._create_analysis_history_table()

    assert captured["table_name"] == analysis_history.name
    items = captured["items"]
    assert isinstance(items, tuple)
    columns = [item for item in items if isinstance(item, Column)]
    assert [column.name for column in columns] == list(analysis_history.c.keys())
    expected_columns = list(analysis_history.c)
    dialect = mysql.dialect()
    for actual, expected in zip(columns, expected_columns, strict=True):
        assert actual.nullable == expected.nullable
        assert actual.primary_key == expected.primary_key
        assert actual.autoincrement == expected.autoincrement
        assert actual.comment == expected.comment
        assert actual.type.compile(dialect=dialect) == expected.type.compile(
            dialect=dialect
        )
        actual_default = (
            None if actual.server_default is None else str(actual.server_default.arg)
        )
        expected_default = (
            None
            if expected.server_default is None
            else str(expected.server_default.arg)
        )
        assert _normalized_default(actual_default) == _normalized_default(
            expected_default
        )

    actual_checks = {
        item.name: str(item.sqltext)
        for item in items
        if isinstance(item, CheckConstraint)
    }
    expected_checks = {
        item.name: str(item.sqltext)
        for item in analysis_history.constraints
        if isinstance(item, CheckConstraint)
    }
    assert actual_checks == expected_checks

    actual_foreign_keys = [
        item for item in items if isinstance(item, ForeignKeyConstraint)
    ]
    assert len(actual_foreign_keys) == 1
    actual_foreign_key = actual_foreign_keys[0]
    expected_foreign_key = next(iter(analysis_history.foreign_key_constraints))
    assert actual_foreign_key.name == expected_foreign_key.name
    assert actual_foreign_key.ondelete == expected_foreign_key.ondelete == "CASCADE"
    assert tuple(actual_foreign_key.column_keys) == (
        "membership_id",
        "user_id",
        "organization_id",
    )
    assert tuple(
        element.target_fullname for element in actual_foreign_key.elements
    ) == tuple(
        element.target_fullname for element in expected_foreign_key.elements
    )

    assert captured["options"] == {
        "comment": analysis_history.comment,
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_0900_ai_ci",
    }
    assert revision.HISTORY_INDEXES == {
        index.name: list(index.columns.keys()) for index in analysis_history.indexes
    }
    assert "result" not in analysis_history.c
    assert "result_rows" not in analysis_history.c
    assert "compiled_sql" not in analysis_history.c


def test_offline_upgrade_and_downgrade_are_explicit_and_fail_closed() -> None:
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "006_invitation_registration -> 007_demo_completion" in upgrade.stdout
    assert "CREATE TABLE analysis_history" in upgrade.stdout
    assert "fk_analysis_history_membership_context" in upgrade.stdout
    assert "ix_analysis_history_context_id" in upgrade.stdout
    assert "ix_analysis_history_context_favorite_id" in upgrade.stdout
    expanded_position = upgrade.stdout.rfind("ck_one_time_tokens_purpose")
    legacy_position = upgrade.stdout.rfind(
        "ck_one_time_tokens_invitation_purpose"
    )
    assert 0 <= expanded_position < legacy_position
    assert "DELETE FROM one_time_tokens" not in upgrade.stdout

    downgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade",
            "007_demo_completion:006_invitation_registration",
            "--sql",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "007_demo_completion -> 006_invitation_registration" in downgrade.stdout
    legacy_position = downgrade.stdout.find(
        "ck_one_time_tokens_invitation_purpose"
    )
    expanded_position = downgrade.stdout.find("ck_one_time_tokens_purpose")
    drop_history_position = downgrade.stdout.find("DROP TABLE analysis_history")
    assert 0 <= legacy_position < expanded_position < drop_history_position
    assert "DELETE FROM one_time_tokens" not in downgrade.stdout


def test_online_upgrade_repairs_partial_state_and_is_idempotent(monkeypatch) -> None:
    revision = _load_revision()
    checks = {revision.LEGACY_PURPOSE_CHECK}
    events: list[tuple[object, ...]] = []

    monkeypatch.setattr(revision.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(revision, "_table_exists", lambda table_name: True)
    monkeypatch.setattr(
        revision,
        "_create_analysis_history_table",
        lambda: pytest.fail("an existing history table must not be recreated"),
    )
    monkeypatch.setattr(
        revision,
        "_ensure_index",
        lambda name, columns, *, offline: events.append(
            ("ensure_index", name, tuple(columns), offline)
        ),
    )
    monkeypatch.setattr(
        revision,
        "_check_exists",
        lambda table_name, constraint_name: constraint_name in checks,
    )

    def create_check(name, table_name, condition):
        events.append(("create_check", name, condition))
        checks.add(name)

    def drop_constraint(name, table_name, *, type_):
        events.append(("drop_check", name, type_))
        checks.remove(name)

    monkeypatch.setattr(revision.op, "create_check_constraint", create_check)
    monkeypatch.setattr(revision.op, "drop_constraint", drop_constraint)

    revision.upgrade()

    assert [event[0] for event in events] == [
        "ensure_index",
        "ensure_index",
        "create_check",
        "drop_check",
    ]
    assert checks == {revision.EXPANDED_PURPOSE_CHECK}

    events.clear()
    revision.upgrade()
    assert [event[0] for event in events] == ["ensure_index", "ensure_index"]


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


def test_downgrade_preflight_rejects_password_reset_rows(monkeypatch) -> None:
    revision = _load_revision()
    connection = _CapturingConnection(1)
    monkeypatch.setattr(revision.op, "get_bind", lambda: connection)

    with pytest.raises(RuntimeError, match="007 downgrade preflight failed"):
        revision._validate_no_non_invitation_tokens()

    assert "purpose <> 'user_invitation'" in connection.sql
    assert "DELETE" not in connection.sql.upper()


def test_downgrade_preflight_allows_invitation_only_rows(monkeypatch) -> None:
    revision = _load_revision()
    connection = _CapturingConnection(0)
    monkeypatch.setattr(revision.op, "get_bind", lambda: connection)

    revision._validate_no_non_invitation_tokens()

    assert "purpose <> 'user_invitation'" in connection.sql


def _normalized_default(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("(", "").replace(")", "")
