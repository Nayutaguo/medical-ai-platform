from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from medical_ai.identity import (
    ALL_IDENTITY_TABLE_NAMES,
    BASE_IDENTITY_TABLE_NAMES,
    FACILITY_SCOPE_TABLE_NAMES,
    IDENTITY_TABLE_NAMES,
    SCOPE_IDENTITY_TABLE_NAMES,
    audit_events,
    auth_sessions,
    background_jobs,
    dataset_versions,
    facilities,
    identity_metadata,
    membership_facility_scopes,
    membership_roles,
    one_time_tokens,
    organization_facilities,
    organization_memberships,
    permissions,
    roles,
    users,
)


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "infra" / "mysql" / "migrations"
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _unique_column_sets(table) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_identity_metadata_contains_required_snake_case_tables_and_columns() -> None:
    assert tuple(identity_metadata.tables) == ALL_IDENTITY_TABLE_NAMES
    assert IDENTITY_TABLE_NAMES == ALL_IDENTITY_TABLE_NAMES
    assert SCOPE_IDENTITY_TABLE_NAMES == FACILITY_SCOPE_TABLE_NAMES
    assert set(BASE_IDENTITY_TABLE_NAMES).isdisjoint(FACILITY_SCOPE_TABLE_NAMES)
    assert identity_metadata.info["timestamp_policy"] == "UTC"
    assert identity_metadata.info["secret_storage_policy"] == "hash_only"
    assert identity_metadata.info["empty_facility_scope_policy"] == "deny_all"

    for table in identity_metadata.tables.values():
        assert SNAKE_CASE.fullmatch(table.name)
        assert "version" in table.c
        assert table.c.version.nullable is False
        assert str(table.c.version.server_default.arg) == "1"
        assert "created_at" in table.c
        assert "UTC_TIMESTAMP" in str(table.c.created_at.server_default.arg)
        for column in table.columns:
            assert SNAKE_CASE.fullmatch(column.name)
        for constraint in table.constraints:
            if constraint.name:
                assert SNAKE_CASE.fullmatch(constraint.name)
                assert len(constraint.name) <= 64
        for index in table.indexes:
            assert SNAKE_CASE.fullmatch(index.name)
            assert len(index.name) <= 64


def test_identity_uniqueness_and_tenant_role_boundaries_are_explicit() -> None:
    assert ("email_normalized",) in _unique_column_sets(users)
    assert ("organization_id", "user_id") in _unique_column_sets(organization_memberships)
    assert ("organization_id", "role_key") in _unique_column_sets(roles)
    assert ("permission_key",) in _unique_column_sets(permissions)
    assert ("resource", "action") in _unique_column_sets(permissions)
    assert ("organization_id", "idempotency_key") in _unique_column_sets(background_jobs)
    assert ("organization_id", "dataset_key", "version_number") in _unique_column_sets(dataset_versions)
    assert users.c.auth_version.nullable is False
    assert str(users.c.auth_version.server_default.arg) == "1"
    assert organization_memberships.c.authorization_version.nullable is False
    assert str(organization_memberships.c.authorization_version.server_default.arg) == "1"

    composite_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in membership_roles.foreign_key_constraints
        if len(constraint.elements) == 2
    }
    assert (
        "organization_memberships.id",
        "organization_memberships.organization_id",
    ) in composite_targets
    assert ("roles.id", "roles.organization_id") in composite_targets

    session_context_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in auth_sessions.foreign_key_constraints
    }
    assert (
        "organization_memberships.id",
        "organization_memberships.user_id",
        "organization_memberships.organization_id",
    ) in session_context_targets


def test_facility_scopes_are_explicit_unique_and_fail_closed() -> None:
    assert ("facility_key",) in _unique_column_sets(facilities)
    assert ("facility_id",) in _unique_column_sets(organization_facilities)
    assert tuple(organization_facilities.primary_key.columns.keys()) == (
        "organization_id",
        "facility_id",
    )
    assert tuple(membership_facility_scopes.primary_key.columns.keys()) == (
        "membership_id",
        "facility_id",
    )
    assert membership_facility_scopes.c.organization_id.nullable is False
    assert "PermanentFacilityId" in facilities.c.facility_key.comment
    assert "No rows means no facility access" in membership_facility_scopes.comment
    for table_name in FACILITY_SCOPE_TABLE_NAMES:
        updated_at = identity_metadata.tables[table_name].c.updated_at
        assert updated_at.onupdate is None
        assert updated_at.server_onupdate is None

    composite_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in membership_facility_scopes.foreign_key_constraints
        if len(constraint.elements) == 2
    }
    assert (
        "organization_memberships.id",
        "organization_memberships.organization_id",
    ) in composite_targets
    assert (
        "organization_facilities.organization_id",
        "organization_facilities.facility_id",
    ) in composite_targets

    # Both composite foreign keys reuse the same non-null organization_id. A
    # membership from one organization therefore cannot be granted a facility
    # that is registered only to another organization.
    assert {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in membership_facility_scopes.foreign_key_constraints
        if len(constraint.elements) == 2
    } == {
        ("membership_id", "organization_id"),
        ("organization_id", "facility_id"),
    }


def test_authentication_secrets_are_hash_only_and_expiring() -> None:
    forbidden_plaintext_columns = {
        "password",
        "session_token",
        "refresh_token",
        "access_token",
        "token",
    }
    all_columns = {
        column.name
        for table in identity_metadata.tables.values()
        for column in table.columns
    }
    assert forbidden_plaintext_columns.isdisjoint(all_columns)

    assert auth_sessions.c.session_token_hash.type.length == 32
    assert auth_sessions.c.refresh_token_hash.type.length == 32
    assert auth_sessions.c.csrf_token_hash.type.length == 32
    assert one_time_tokens.c.token_hash.type.length == 32
    assert background_jobs.c.lease_token_hash.type.length == 32
    assert auth_sessions.c.expires_at.nullable is False
    assert auth_sessions.c.idle_expires_at.nullable is False
    assert auth_sessions.c.membership_id.nullable is False
    assert auth_sessions.c.identity_version.nullable is False
    assert auth_sessions.c.authorization_version.nullable is False
    assert one_time_tokens.c.expires_at.nullable is False
    assert ("session_token_hash",) in _unique_column_sets(auth_sessions)
    assert ("csrf_token_hash",) in _unique_column_sets(auth_sessions)
    assert ("token_hash",) in _unique_column_sets(one_time_tokens)


def test_audit_and_dataset_foreign_keys_enforce_tenant_context() -> None:
    assert audit_events.foreign_keys
    assert {foreign_key.ondelete for foreign_key in audit_events.foreign_keys} == {
        "RESTRICT"
    }
    audit_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in audit_events.foreign_key_constraints
    }
    assert (
        "organization_memberships.id",
        "organization_memberships.user_id",
        "organization_memberships.organization_id",
    ) in audit_targets
    assert audit_events.c.actor_kind.nullable is False
    assert audit_events.c.actor_membership_id.nullable is True

    dataset_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in dataset_versions.foreign_key_constraints
    }
    assert (
        "background_jobs.id",
        "background_jobs.organization_id",
    ) in dataset_targets
    assert ("id", "organization_id") in _unique_column_sets(background_jobs)


def test_mysql_ddl_compiles_with_indexes_foreign_keys_and_utc_defaults() -> None:
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=mysql.dialect()))
        for table in identity_metadata.sorted_tables
    )

    assert "UTC_TIMESTAMP(6)" in ddl
    assert "FOREIGN KEY" in ddl
    assert "UNIQUE" in ddl
    assert "session_token_hash BINARY(32)" in ddl
    assert "csrf_token_hash BINARY(32)" in ddl
    assert "token_hash BINARY(32)" in ddl
    assert "session_token VARCHAR" not in ddl
    assert "token VARCHAR" not in ddl


def test_002_migration_remains_immutable_and_down_is_reverse_order() -> None:
    up_sql = (MIGRATIONS / "002_identity_control_plane.up.sql").read_text(encoding="utf-8")
    down_sql = (MIGRATIONS / "002_identity_control_plane.down.sql").read_text(encoding="utf-8")

    created = re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", up_sql, flags=re.IGNORECASE)
    dropped = re.findall(r"DROP TABLE IF EXISTS\s+([a-z_]+)", down_sql, flags=re.IGNORECASE)

    assert created == list(BASE_IDENTITY_TABLE_NAMES)
    assert dropped == list(reversed(BASE_IDENTITY_TABLE_NAMES))
    assert "CREATE INDEX" not in up_sql.upper()
    assert "DEFAULT (UTC_TIMESTAMP(6))" in up_sql
    assert "session_token_hash BINARY(32)" in up_sql
    assert "csrf_token_hash BINARY(32)" in up_sql
    assert "token_hash BINARY(32)" in up_sql
    assert not re.search(
        r"^\s{2}(?:password|session_token|refresh_token|csrf_token|access_token|token)\s+",
        up_sql,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    table_blocks = {
        name: body
        for name, body in re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)\s*\((.*?)\n\)",
            up_sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    ignored_line_starts = {"primary", "constraint", "key", "unique", "foreign", "references"}
    for table_name in BASE_IDENTITY_TABLE_NAMES:
        table = identity_metadata.tables[table_name]
        migration_columns = []
        for line in table_blocks[table_name].splitlines():
            if not line.startswith("  ") or line.startswith("    "):
                continue
            first_word = line.strip().split(maxsplit=1)[0].rstrip(",").lower()
            if first_word not in ignored_line_starts:
                migration_columns.append(first_word)
        metadata_columns = [column.name for column in table.columns]
        if table_name == "audit_events":
            metadata_columns.remove("actor_kind")
            metadata_columns.remove("actor_membership_id")
        if table_name == "one_time_tokens":
            # Revision 006 adds tenant and identity bindings while the
            # deployed 002 baseline remains immutable.
            metadata_columns.remove("organization_id")
            metadata_columns.remove("membership_id")
            metadata_columns.remove("identity_version")
        assert migration_columns == metadata_columns

    metadata_constraint_names = {
        constraint.name
        for table_name in BASE_IDENTITY_TABLE_NAMES
        for table in (identity_metadata.tables[table_name],)
        for constraint in table.constraints
        if constraint.name and not isinstance(constraint, PrimaryKeyConstraint)
    }
    metadata_constraint_names.difference_update(
        {
            "uq_background_jobs_id_organization",
            "fk_dataset_versions_import_job_org",
            "fk_audit_events_actor_membership_context",
            "ck_audit_events_actor_kind",
            "ck_audit_events_actor_context",
            "fk_one_time_tokens_membership_context",
            "ck_one_time_tokens_invitation_purpose",
            "ck_one_time_tokens_purpose",
            "ck_one_time_tokens_identity_version_positive",
        }
    )
    metadata_constraint_names.update(
        {
            "fk_dataset_versions_import_job",
            "fk_audit_events_actor_user",
            "fk_one_time_tokens_user",
        }
    )
    migration_constraint_names = set(
        re.findall(r"\bCONSTRAINT\s+([a-z_]+)", up_sql, flags=re.IGNORECASE)
    )
    assert migration_constraint_names == metadata_constraint_names

    metadata_index_names = {
        index.name
        for table_name in BASE_IDENTITY_TABLE_NAMES
        for table in (identity_metadata.tables[table_name],)
        for index in table.indexes
    }
    metadata_index_names.difference_update(
        {
            "ix_audit_events_actor_context",
            "ix_audit_events_membership_occurred",
            "ix_one_time_tokens_organization_purpose_expires",
        }
    )
    migration_index_names = set(
        re.findall(r"^\s+KEY\s+([a-z_]+)", up_sql, flags=re.IGNORECASE | re.MULTILINE)
    )
    assert migration_index_names == metadata_index_names

    audit_block = table_blocks["audit_events"].upper()
    assert "ON DELETE CASCADE" not in audit_block
    assert audit_block.count("ON DELETE SET NULL") == 2


def test_005_security_migration_is_explicit_fail_closed_and_reversible() -> None:
    up_sql = (MIGRATIONS / "005_control_plane_security.up.sql").read_text(
        encoding="utf-8"
    )
    down_sql = (MIGRATIONS / "005_control_plane_security.down.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN actor_kind VARCHAR(16) NULL" in up_sql
    assert "ADD COLUMN actor_membership_id CHAR(36) NULL" in up_sql
    assert "MODIFY COLUMN actor_kind VARCHAR(16) NOT NULL" in up_sql
    assert "fk_audit_events_actor_membership_context" in up_sql
    assert "(actor_membership_id, actor_user_id, organization_id)" in up_sql
    assert "REFERENCES organization_memberships (id, user_id, organization_id)" in up_sql
    assert "ck_audit_events_actor_context" in up_sql
    assert "actor_kind = 'system'" in up_sql
    assert "actor_membership_id IS NULL" in up_sql
    assert "actor_user_id IS NULL" in up_sql
    assert "uq_background_jobs_id_organization" in up_sql
    assert "uq_organization_facilities_facility UNIQUE (facility_id)" in up_sql
    assert "DROP INDEX ix_organization_facilities_facility" in up_sql
    assert "fk_dataset_versions_import_job_org" in up_sql
    assert "(import_job_id, organization_id)" in up_sql
    assert "REFERENCES background_jobs (id, organization_id)" in up_sql
    assert up_sql.count("ON DELETE RESTRICT") == 3

    assert "fk_dataset_versions_import_job_org" in down_sql
    assert "fk_dataset_versions_import_job" in down_sql
    assert "fk_audit_events_actor_user" in down_sql
    assert "ADD KEY ix_organization_facilities_facility (facility_id)" in down_sql
    assert "DROP INDEX uq_organization_facilities_facility" in down_sql
    assert "DROP COLUMN actor_membership_id" in down_sql
    assert "DROP COLUMN actor_kind" in down_sql


def test_facility_scope_migration_matches_scope_metadata_and_is_reversible() -> None:
    up_sql = (MIGRATIONS / "003_facility_scopes.up.sql").read_text(encoding="utf-8")
    down_sql = (MIGRATIONS / "003_facility_scopes.down.sql").read_text(encoding="utf-8")

    created = re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", up_sql, flags=re.IGNORECASE)
    dropped = re.findall(r"DROP TABLE IF EXISTS\s+([a-z_]+)", down_sql, flags=re.IGNORECASE)
    assert created == list(FACILITY_SCOPE_TABLE_NAMES)
    assert dropped == list(reversed(FACILITY_SCOPE_TABLE_NAMES))
    assert "ON UPDATE" not in up_sql.upper()
    assert "DEFAULT (UTC_TIMESTAMP(6))" in up_sql
    assert "no rows means no facility access" in up_sql.lower()

    table_blocks = {
        name: body
        for name, body in re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)\s*\((.*?)\n\)",
            up_sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    ignored_line_starts = {"primary", "constraint", "key", "unique", "foreign", "references"}
    for table_name in FACILITY_SCOPE_TABLE_NAMES:
        table = identity_metadata.tables[table_name]
        migration_columns = []
        for line in table_blocks[table_name].splitlines():
            if not line.startswith("  ") or line.startswith("    "):
                continue
            first_word = line.strip().split(maxsplit=1)[0].rstrip(",").lower()
            if first_word not in ignored_line_starts:
                migration_columns.append(first_word)
        assert migration_columns == [column.name for column in table.columns]

    metadata_constraint_names = {
        constraint.name
        for table_name in FACILITY_SCOPE_TABLE_NAMES
        for constraint in identity_metadata.tables[table_name].constraints
        if constraint.name and not isinstance(constraint, PrimaryKeyConstraint)
    }
    metadata_constraint_names.remove("uq_organization_facilities_facility")
    migration_constraint_names = set(
        re.findall(r"\bCONSTRAINT\s+([a-z_]+)", up_sql, flags=re.IGNORECASE)
    )
    assert migration_constraint_names == metadata_constraint_names

    metadata_index_names = {
        index.name
        for table_name in FACILITY_SCOPE_TABLE_NAMES
        for index in identity_metadata.tables[table_name].indexes
    }
    metadata_index_names.add("ix_organization_facilities_facility")
    migration_index_names = set(
        re.findall(r"^\s+KEY\s+([a-z_]+)", up_sql, flags=re.IGNORECASE | re.MULTILINE)
    )
    assert migration_index_names == metadata_index_names

    scope_block = table_blocks["membership_facility_scopes"]
    assert "FOREIGN KEY (membership_id, organization_id)" in scope_block
    assert "REFERENCES organization_memberships (id, organization_id)" in scope_block
    assert "FOREIGN KEY (organization_id, facility_id)" in scope_block
    assert "REFERENCES organization_facilities (organization_id, facility_id)" in scope_block
