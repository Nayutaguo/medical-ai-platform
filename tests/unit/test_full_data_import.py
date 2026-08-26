from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from medical_ai.config import Settings
from medical_ai.data import CANONICAL_COLUMNS
from medical_ai.data.cleaning import SPARCS_SCHEMA_VERSION
from medical_ai.pipeline import full_import as full_import_module
from medical_ai.pipeline.full_import import (
    FullDataImporter,
    ImportOptions,
    ImportPipelineError,
    SourceManifestError,
    TableIdentity,
    atomic_write_json,
    build_import_table_names,
    build_table_marker,
    default_audit_path,
    load_clean_source,
)


def _write_clean_artifacts(
    tmp_path: Path,
    *,
    source_complete: bool = True,
    header: list[str] | None = None,
    row: list[str] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    csv_path = tmp_path / "clean.csv"
    canonical_header = header if header is not None else list(CANONICAL_COLUMNS)
    canonical_row = row if row is not None else [""] * len(CANONICAL_COLUMNS)
    csv_path.write_text(
        ",".join(canonical_header) + "\n" + ",".join(canonical_row) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest: dict[str, object] = {
        "task_id": uuid4().hex,
        "status": "succeeded",
        "schema_version": SPARCS_SCHEMA_VERSION,
        "source": {
            "path": str(tmp_path / "raw.csv"),
            "size_bytes": 100,
            "mtime_ns": 1,
            "sha256": "a" * 64,
        },
        "output": {
            "path": str(csv_path.resolve()),
            "row_count": 1,
            "sha256": digest,
            "published": True,
        },
        "quality": {
            "input_rows": 1,
            "output_rows": 1,
            "rejected_rows": 0,
            "max_rejected_rows": 0,
            "schema_valid": True,
            "row_counts_reconciled": True,
            "source_complete": source_complete,
            "min_output_rows": 1,
        },
        "started_at": "2026-08-25T00:00:00+00:00",
        "finished_at": "2026-08-25T00:00:01+00:00",
        "error": None,
    }
    manifest_path = tmp_path / "clean.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return csv_path, manifest_path, manifest


def _accepted_source(tmp_path: Path) -> Any:
    csv_path, manifest_path, _ = _write_clean_artifacts(tmp_path)
    return load_clean_source(csv_path, manifest_path=manifest_path)


def _latest_audit_record(path: Path) -> dict[str, Any]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records
    return records[-1]


def test_load_clean_source_requires_successful_complete_manifest(tmp_path: Path) -> None:
    csv_path, manifest_path, _ = _write_clean_artifacts(tmp_path)

    source = load_clean_source(csv_path, manifest_path=manifest_path)

    assert source.row_count == 1
    assert source.schema_version == SPARCS_SCHEMA_VERSION
    assert source.raw_source_sha256 == "a" * 64
    assert source.manifest_sha256
    assert source.source_complete is True
    assert source.inode is not None


def test_explicit_rows_hash_and_partial_overrides_are_disabled(tmp_path: Path) -> None:
    csv_path, manifest_path, _ = _write_clean_artifacts(tmp_path)

    with pytest.raises(SourceManifestError, match="row/hash"):
        load_clean_source(csv_path, manifest_path=manifest_path, expected_rows=1)
    with pytest.raises(SourceManifestError, match="row/hash"):
        load_clean_source(csv_path, expected_sha256="a" * 64)
    with pytest.raises(SourceManifestError, match="partial"):
        load_clean_source(
            csv_path, manifest_path=manifest_path, allow_partial_source=True
        )


def test_source_without_manifest_is_never_accepted(tmp_path: Path) -> None:
    csv_path, _, _ = _write_clean_artifacts(tmp_path)

    with pytest.raises(SourceManifestError, match="manifest"):
        load_clean_source(csv_path)


def test_partial_manifest_is_rejected_without_any_override(tmp_path: Path) -> None:
    csv_path, manifest_path, _ = _write_clean_artifacts(
        tmp_path, source_complete=False
    )

    with pytest.raises(SourceManifestError, match="partial"):
        load_clean_source(csv_path, manifest_path=manifest_path)


def test_load_clean_source_rejects_tampered_csv(tmp_path: Path) -> None:
    csv_path, manifest_path, _ = _write_clean_artifacts(tmp_path)
    contents = csv_path.read_text(encoding="utf-8")
    header, data_row = contents.splitlines()
    csv_path.write_text(header + "\n" + "x" + data_row + "\n", encoding="utf-8")

    with pytest.raises(SourceManifestError, match="SHA-256"):
        load_clean_source(csv_path, manifest_path=manifest_path)


def test_load_clean_source_rejects_inconsistent_manifest_rows(tmp_path: Path) -> None:
    csv_path, manifest_path, manifest = _write_clean_artifacts(tmp_path)
    quality = manifest["quality"]
    assert isinstance(quality, dict)
    quality["input_rows"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SourceManifestError, match="row counts"):
        load_clean_source(csv_path, manifest_path=manifest_path)


def test_manifest_revalidation_compares_raw_source_provenance(tmp_path: Path) -> None:
    source = _accepted_source(tmp_path)
    assert source.manifest_path is not None
    manifest = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["sha256"] = "b" * 64
    source.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    stale_source = replace(
        source,
        manifest_sha256=hashlib.sha256(source.manifest_path.read_bytes()).hexdigest(),
    )
    importer = FullDataImporter(
        _EngineStub(),
        Settings(app_environment="test", mysql_database="test_import"),
    )

    with pytest.raises(SourceManifestError, match="no longer matches"):
        importer._verify_manifest_binding(stale_source)


@pytest.mark.parametrize(
    ("header", "row"),
    [
        ([*CANONICAL_COLUMNS, "Unexpected"], [""] * (len(CANONICAL_COLUMNS) + 1)),
        (list(CANONICAL_COLUMNS), [""] * (len(CANONICAL_COLUMNS) - 1)),
        (list(CANONICAL_COLUMNS), [""] * (len(CANONICAL_COLUMNS) + 1)),
    ],
)
def test_canonical_header_and_every_row_width_are_strict(
    tmp_path: Path, header: list[str], row: list[str]
) -> None:
    csv_path, manifest_path, _ = _write_clean_artifacts(
        tmp_path, header=header, row=row
    )

    with pytest.raises(SourceManifestError, match="canonical"):
        load_clean_source(csv_path, manifest_path=manifest_path)


def test_generated_table_names_and_marker_are_internal_uuid_values() -> None:
    task_id = "3f76c0d2-24f3-4e8c-9658-97049ca11241"
    names = build_import_table_names(task_id)
    marker = build_table_marker(task_id, "b" * 64)

    assert names.staging == "inpatient_staging_3f76c0d224f34e8c965897049ca11241"
    assert names.backup == "inpatient_backup_3f76c0d224f34e8c965897049ca11241"
    assert names.failed == "inpatient_failed_3f76c0d224f34e8c965897049ca11241"
    assert len(names.staging) <= 64
    assert "task_id=3f76c0d224f34e8c965897049ca11241" in marker
    assert marker.endswith("source_sha256=" + "b" * 64)

    with pytest.raises(ValueError, match="UUID"):
        build_import_table_names("inpatient; DROP TABLE users")


def test_import_options_are_bounded_and_backup_drop_is_forbidden() -> None:
    task_id = str(uuid4())
    assert ImportOptions(task_id=task_id).keep_backup is True

    with pytest.raises(ValueError, match="batch_size"):
        ImportOptions(task_id=task_id, batch_size=1)
    with pytest.raises(ValueError, match="forbidden"):
        ImportOptions(task_id=task_id, keep_backup=False)


def test_default_audit_is_task_specific_and_atomic_updates_remain_private(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "clean.csv"
    first = default_audit_path(csv_path, str(uuid4()))
    second = default_audit_path(csv_path, str(uuid4()))
    assert first != second
    assert first != csv_path.resolve()

    first.write_text("old", encoding="utf-8")
    atomic_write_json(first, {"status": "succeeded", "rows": 2_101_588})
    assert json.loads(first.read_text(encoding="utf-8"))["rows"] == 2_101_588
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert list(tmp_path.glob(f".{first.name}.*.tmp")) == []


def test_guarded_audit_update_refuses_replaced_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "guarded.audit.json"
    identity = full_import_module._create_json_exclusive(
        path, {"task_id": str(uuid4()), "status": "validating"}
    )
    path.unlink()
    path.write_text("replacement", encoding="utf-8")

    with pytest.raises(ImportPipelineError) as caught:
        full_import_module._guarded_write_json(
            path, {"status": "loading"}, identity
        )

    assert caught.value.code == "AUDIT_OWNERSHIP_LOST"
    assert path.read_text(encoding="utf-8") == "replacement"


def test_guarded_audit_update_never_writes_concurrent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "guarded.audit.json"
    identity = full_import_module._create_json_exclusive(
        path, {"task_id": str(uuid4()), "status": "validating"}
    )
    moved_owned_inode = tmp_path / "owned-inode.json"
    replacement = b"replacement-must-not-be-overwritten"
    original_append = full_import_module._append_json_descriptor

    def replace_path_then_write(descriptor: int, payload: Any) -> None:
        path.replace(moved_owned_inode)
        path.write_bytes(replacement)
        original_append(descriptor, payload)

    monkeypatch.setattr(
        full_import_module, "_append_json_descriptor", replace_path_then_write
    )

    with pytest.raises(ImportPipelineError) as caught:
        full_import_module._guarded_write_json(
            path, {"status": "loading"}, identity
        )

    assert caught.value.code == "AUDIT_OWNERSHIP_LOST"
    assert path.read_bytes() == replacement
    assert _latest_audit_record(moved_owned_inode)["status"] == "loading"


def test_append_only_audit_preserves_last_complete_checkpoint_on_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "append-only.audit.jsonl"
    initial = {"task_id": str(uuid4()), "status": "validating"}
    identity = full_import_module._create_json_exclusive(path, initial)

    def append_partial_then_fail(descriptor: int, payload: Any) -> None:
        os.write(descriptor, b'{"incomplete"')
        os.fsync(descriptor)
        raise OSError("simulated crash boundary")

    monkeypatch.setattr(
        full_import_module, "_append_json_descriptor", append_partial_then_fail
    )

    with pytest.raises(ImportPipelineError) as caught:
        full_import_module._guarded_write_json(path, {"status": "loading"}, identity)

    assert caught.value.code == "AUDIT_PERSIST_FAILED"
    first_line = path.read_bytes().splitlines()[0]
    assert json.loads(first_line)["status"] == "validating"


def test_staging_create_ddl_contains_current_marker() -> None:
    task_id = str(uuid4())
    marker = build_table_marker(task_id, "b" * 64)
    table = full_import_module._staging_table(
        build_import_table_names(task_id).staging,
        marker=marker,
    )

    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert table.comment == marker
    assert f"COMMENT='{marker}'" in ddl
    assert "CREATE TABLE inpatient_staging_" in ddl


def test_task_private_snapshot_survives_atomic_source_replacement(
    tmp_path: Path,
) -> None:
    source = _accepted_source(tmp_path)
    importer = FullDataImporter(
        _EngineStub(),
        Settings(app_environment="test", mysql_database="test_import"),
    )
    snapshot = importer._create_source_snapshot(source, str(uuid4()))
    replacement_path = tmp_path / "replacement.csv"
    replacement_path.write_text(
        ",".join(CANONICAL_COLUMNS)
        + "\n"
        + ",".join(["x", *([""] * (len(CANONICAL_COLUMNS) - 1))])
        + "\n",
        encoding="utf-8",
    )
    replacement_path.replace(source.csv_path)

    verification = importer._verify_source_snapshot(snapshot)

    assert verification.sha256 == source.sha256
    assert snapshot.csv_path.stat().st_ino != source.csv_path.stat().st_ino
    assert importer._cleanup_source_snapshot(snapshot) == "removed_owned_snapshot"
    assert not snapshot.csv_path.exists()


class _LockConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _EngineStub:
    def __init__(self) -> None:
        self.connections: list[_LockConnection] = []

    def connect(self) -> _LockConnection:
        connection = _LockConnection()
        self.connections.append(connection)
        return connection


class _StateMachineImporter(FullDataImporter):
    def __init__(
        self,
        *,
        reconcile_state: str = "published",
        rename_raises: bool = False,
        lock_busy: bool = False,
        mutate_during_load: bool = False,
        cleanup_raises: bool = False,
        live_existed: bool = True,
        quarantine_state: str = "quarantined",
        create_raises: bool = False,
    ) -> None:
        self.stub_engine = _EngineStub()
        super().__init__(
            self.stub_engine,
            Settings(app_environment="test", mysql_database="test_import"),
        )
        self.reconcile_state = reconcile_state
        self.rename_raises = rename_raises
        self.lock_busy = lock_busy
        self.mutate_during_load = mutate_during_load
        self.cleanup_raises = cleanup_raises
        self.live_existed = live_existed
        self.quarantine_state = quarantine_state
        self.create_raises = create_raises
        self.events: list[str] = []

    def _acquire_import_lock(self, connection: Any) -> None:
        self.events.append("lock_acquire")
        if self.lock_busy:
            raise ImportPipelineError("IMPORT_LOCK_BUSY", "busy")

    def _release_import_lock(self, connection: Any) -> None:
        self.events.append("lock_release")

    def _assert_import_lock_owned(self, connection: Any) -> None:
        self.events.append("lock_owned")

    def _inspect_initial_state(
        self, connection: Any, names: Any
    ) -> tuple[bool, TableIdentity | None]:
        self.events.append("inspect")
        return (
            self.live_existed,
            TableIdentity(7, "legacy", "schema")
            if self.live_existed
            else None,
        )

    def _create_staging(self, table_name: str, *, marker: str) -> None:
        self.events.append("create")
        if self.create_raises:
            raise ImportPipelineError("STAGING_CREATE_ACK_LOST", "simulated")

    def _assert_owned_table(self, table_name: str, marker: str) -> None:
        self.events.append("owned")

    def _load_source(
        self,
        source: Any,
        table_name: str,
        options: Any,
        checkpoint: Any,
        audit: Any,
    ) -> tuple[str, int]:
        self.events.append("load")
        # Exercise the mandatory pre-load stat+SHA+shape verification.
        self._verify_source_snapshot(source)
        checkpoint(source.row_count, 1)
        if self.mutate_during_load:
            contents = source.csv_path.read_text(encoding="utf-8")
            source.csv_path.write_text(contents[:-2] + "x\n", encoding="utf-8")
        return "insert", source.row_count

    def _validate_staging(
        self, table_name: str, marker: str, expected_rows: int, loaded_rows: int
    ) -> int:
        self.events.append("validate")
        return expected_rows

    def _ensure_indexes(self, table_name: str) -> None:
        self.events.append("indexes")

    def _assert_publish_preconditions(self, names: Any, **kwargs: Any) -> None:
        self.events.append("prepublish")

    def _publish(
        self, names: Any, *, live_existed: bool, connection: Any
    ) -> None:
        self.events.append("rename")
        if self.rename_raises:
            raise ConnectionError("acknowledgement lost")

    def _reconcile_publish(self, names: Any, **kwargs: Any) -> dict[str, Any]:
        self.events.append("reconcile")
        return {
            "state": self.reconcile_state,
            "live_rows": kwargs["expected_rows"]
            if self.reconcile_state in {"published", "published_invalid"}
            else None,
            "tables": {},
        }

    def _cleanup_owned_staging(self, table_name: str, marker: str) -> str:
        self.events.append("cleanup")
        if self.cleanup_raises:
            raise ImportPipelineError("CLEANUP_TEST_FAILURE", "cleanup failed")
        return "dropped_owned_staging"

    def _quarantine_failed_first_publish(
        self, names: Any, *, marker: str, connection: Any
    ) -> dict[str, Any]:
        self.events.append("quarantine")
        return {"state": self.quarantine_state}


def test_state_machine_success_releases_lock_and_retains_backup(tmp_path: Path) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter()
    audit_path = default_audit_path(source.csv_path, str(uuid4()))
    options = ImportOptions(task_id=str(uuid4()), method="insert")

    result = importer.run(source, options, audit_path=audit_path)
    audit = _latest_audit_record(audit_path)

    assert result.status == "succeeded"
    assert result.backup_table is not None
    assert audit["status"] == "succeeded"
    assert audit["backup_retained"] is True
    assert audit["lock"]["released"] is True
    assert importer.events[-1] == "lock_release"


def test_lost_rename_ack_is_reconciled_as_success_on_fresh_state(
    tmp_path: Path,
) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(rename_raises=True, reconcile_state="published")
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    result = importer.run(
        source,
        ImportOptions(task_id=task_id, method="insert"),
        audit_path=audit_path,
    )
    audit = _latest_audit_record(audit_path)

    assert result.status == "succeeded"
    assert audit["publish_reconciliation"]["rename_acknowledged"] is False
    assert audit["publish_reconciliation"]["state"] == "published"
    assert "lock_release" in importer.events


def test_unknown_publish_state_never_cleans_or_blindly_restores(tmp_path: Path) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(rename_raises=True, reconcile_state="unknown")
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    with pytest.raises(ImportPipelineError) as caught:
        importer.run(
            source,
            ImportOptions(task_id=task_id, method="insert"),
            audit_path=audit_path,
        )

    audit = _latest_audit_record(audit_path)
    assert caught.value.code == "PUBLISH_STATE_UNKNOWN"
    assert audit["status"] == "unknown"
    assert audit["cleanup"]["staging"]["status"] == "skipped_unknown_publish_state"
    assert "cleanup" not in importer.events
    assert "lock_release" in importer.events


def test_invalid_first_publish_is_quarantined_instead_of_left_live(
    tmp_path: Path,
) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(
        reconcile_state="published_invalid",
        live_existed=False,
    )
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    with pytest.raises(ImportPipelineError) as caught:
        importer.run(
            source,
            ImportOptions(task_id=task_id, method="insert"),
            audit_path=audit_path,
        )

    audit = _latest_audit_record(audit_path)
    assert caught.value.code == "POST_PUBLISH_VERIFICATION_FAILED_QUARANTINED"
    assert audit["status"] == "failed"
    assert audit["published"] is False
    assert audit["quarantine_reconciliation"]["state"] == "quarantined"
    assert "quarantine" in importer.events
    assert "cleanup" not in importer.events


def test_lock_contention_is_stable_and_connection_is_closed(tmp_path: Path) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(lock_busy=True)
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    with pytest.raises(ImportPipelineError) as caught:
        importer.run(
            source,
            ImportOptions(task_id=task_id, dry_run=True),
            audit_path=audit_path,
        )

    assert caught.value.code == "IMPORT_LOCK_BUSY"
    assert importer.events == ["lock_acquire"]
    assert importer.stub_engine.connections[0].closed is True


def test_post_load_stat_and_sha_recheck_catches_toctou_and_cleans_owned_staging(
    tmp_path: Path,
) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(mutate_during_load=True)
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    with pytest.raises(ImportPipelineError) as caught:
        importer.run(
            source,
            ImportOptions(task_id=task_id, method="insert"),
            audit_path=audit_path,
        )

    audit = _latest_audit_record(audit_path)
    assert caught.value.code == "SOURCE_CHANGED_DURING_IMPORT"
    assert audit["cleanup"]["staging"]["attempted"] is True
    assert audit["cleanup"]["staging"]["status"] == "dropped_owned_staging"
    assert "rename" not in importer.events


def test_cleanup_failure_is_persisted_without_masking_primary_failure(
    tmp_path: Path,
) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(
        reconcile_state="not_published", cleanup_raises=True
    )
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    with pytest.raises(ImportPipelineError) as caught:
        importer.run(
            source,
            ImportOptions(task_id=task_id, method="insert"),
            audit_path=audit_path,
        )

    audit = _latest_audit_record(audit_path)
    assert caught.value.code == "PUBLISH_NOT_APPLIED"
    assert audit["cleanup"]["staging"] == {
        "attempted": True,
        "status": "failed",
        "error_code": "CLEANUP_TEST_FAILURE",
    }


def test_create_ack_failure_enters_owned_cleanup_state(tmp_path: Path) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter(create_raises=True)
    task_id = str(uuid4())
    audit_path = default_audit_path(source.csv_path, task_id)

    with pytest.raises(ImportPipelineError) as caught:
        importer.run(
            source,
            ImportOptions(task_id=task_id, method="insert"),
            audit_path=audit_path,
        )

    audit = _latest_audit_record(audit_path)
    assert caught.value.code == "STAGING_CREATE_ACK_LOST"
    assert audit["cleanup"]["staging"]["attempted"] is True
    assert audit["cleanup"]["staging"]["status"] == "dropped_owned_staging"


def test_audit_path_must_be_distinct_and_must_not_already_exist(tmp_path: Path) -> None:
    source = _accepted_source(tmp_path)
    importer = _StateMachineImporter()
    options = ImportOptions(task_id=str(uuid4()), dry_run=True)

    with pytest.raises(ImportPipelineError) as same_csv:
        importer.run(source, options, audit_path=source.csv_path)
    assert same_csv.value.code == "AUDIT_PATH_INVALID"

    assert source.manifest_path is not None
    with pytest.raises(ImportPipelineError) as same_manifest:
        importer.run(source, options, audit_path=source.manifest_path)
    assert same_manifest.value.code == "AUDIT_PATH_INVALID"

    with pytest.raises(ImportPipelineError) as same_profile:
        importer.run(
            source,
            options,
            audit_path=source.csv_path.with_suffix(".profile.json"),
        )
    assert same_profile.value.code == "AUDIT_PATH_INVALID"

    existing = tmp_path / "existing.audit.json"
    existing.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ImportPipelineError) as conflict:
        importer.run(source, options, audit_path=existing)
    assert conflict.value.code == "AUDIT_PATH_CONFLICT"
    assert existing.read_text(encoding="utf-8") == "do not overwrite"


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _SqlRecorder:
    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)
        self.statements: list[str] = []

    def execute(self, statement: Any, parameters: Any = None) -> _ScalarResult:
        self.statements.append(str(statement))
        return _ScalarResult(self.values.pop(0))


def test_named_lock_uses_get_lock_and_release_lock() -> None:
    importer = FullDataImporter(
        _EngineStub(),
        Settings(app_environment="test", mysql_database="test_import"),
    )
    connection = _SqlRecorder([1, 1, 1])

    importer._acquire_import_lock(connection)
    importer._assert_import_lock_owned(connection)
    importer._release_import_lock(connection)

    assert "GET_LOCK" in connection.statements[0]
    assert "IS_USED_LOCK" in connection.statements[1]
    assert "CONNECTION_ID" in connection.statements[1]
    assert "RELEASE_LOCK" in connection.statements[2]


def test_named_lock_ownership_loss_fails_closed() -> None:
    importer = FullDataImporter(
        _EngineStub(),
        Settings(app_environment="test", mysql_database="test_import"),
    )

    with pytest.raises(ImportPipelineError) as caught:
        importer._assert_import_lock_owned(_SqlRecorder([0]))

    assert caught.value.code == "IMPORT_LOCK_LOST"


@pytest.mark.parametrize("conflict_label", ["staging", "backup", "failed"])
def test_initial_state_checks_all_task_table_conflicts(
    monkeypatch: pytest.MonkeyPatch, conflict_label: str
) -> None:
    importer = FullDataImporter(
        _EngineStub(),
        Settings(app_environment="test", mysql_database="test_import"),
    )
    names = build_import_table_names(str(uuid4()))
    conflict_name = getattr(names, conflict_label)

    monkeypatch.setattr(
        full_import_module,
        "_table_exists",
        lambda connection, schema, table: table == conflict_name,
    )

    with pytest.raises(ImportPipelineError) as caught:
        importer._inspect_initial_state(object(), names)
    assert caught.value.code == "IMPORT_TABLE_CONFLICT"


class _ContextConnection:
    def __init__(self) -> None:
        self.execute_calls = 0

    def __enter__(self) -> "_ContextConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, statement: Any, parameters: Any = None) -> None:
        self.execute_calls += 1
        return None


class _ContextEngine:
    def __init__(self) -> None:
        self.begin_calls = 0

    def connect(self) -> _ContextConnection:
        return _ContextConnection()

    def begin(self) -> _ContextConnection:
        self.begin_calls += 1
        return _ContextConnection()


class _OwnershipImporter(FullDataImporter):
    def __init__(self, snapshots: list[dict[str, dict[str, bool]]]) -> None:
        self.context_engine = _ContextEngine()
        super().__init__(
            self.context_engine,
            Settings(app_environment="test", mysql_database="test_import"),
        )
        self.snapshots = list(snapshots)
        self.identity = TableIdentity(7, "legacy", "schema")

    def _table_facts(
        self, connection: Any, names: Any, marker: str
    ) -> dict[str, dict[str, bool]]:
        return self.snapshots.pop(0)

    def _table_identity(self, connection: Any, table_name: str) -> TableIdentity:
        return self.identity


def _facts(
    *, live: tuple[bool, bool], staging: tuple[bool, bool],
    backup: tuple[bool, bool], failed: tuple[bool, bool]
) -> dict[str, dict[str, bool]]:
    return {
        "live": {"exists": live[0], "owned": live[1]},
        "staging": {"exists": staging[0], "owned": staging[1]},
        "backup": {"exists": backup[0], "owned": backup[1]},
        "failed": {"exists": failed[0], "owned": failed[1]},
    }


def test_restore_refuses_ddl_when_current_live_is_not_owned() -> None:
    importer = _OwnershipImporter(
        [
            _facts(
                live=(True, False),
                staging=(False, False),
                backup=(True, False),
                failed=(False, False),
            )
        ]
    )
    names = build_import_table_names(str(uuid4()))
    connection = _ContextConnection()

    result = importer._restore_previous_live(
        names,
        marker="owned-marker",
        previous_live_identity=importer.identity,
        connection=connection,
    )

    assert result["state"] == "unknown"
    assert connection.execute_calls == 0


def test_restore_preserves_failed_table_and_reconciles_lost_ack() -> None:
    before = _facts(
        live=(True, True),
        staging=(False, False),
        backup=(True, False),
        failed=(False, False),
    )
    after = _facts(
        live=(True, False),
        staging=(False, False),
        backup=(False, False),
        failed=(True, True),
    )
    importer = _OwnershipImporter([before, after])
    names = build_import_table_names(str(uuid4()))
    connection = _ContextConnection()

    result = importer._restore_previous_live(
        names,
        marker="owned-marker",
        previous_live_identity=importer.identity,
        connection=connection,
    )

    assert result["state"] == "restored"
    assert result["failed_table_retained"] is True
    assert connection.execute_calls == 1


def test_restore_refuses_backup_that_does_not_match_previous_live() -> None:
    before = _facts(
        live=(True, True),
        staging=(False, False),
        backup=(True, False),
        failed=(False, False),
    )
    importer = _OwnershipImporter([before])
    connection = _ContextConnection()

    result = importer._restore_previous_live(
        build_import_table_names(str(uuid4())),
        marker="owned-marker",
        previous_live_identity=TableIdentity(8, "different", "different"),
        connection=connection,
    )

    assert result["state"] == "unknown"
    assert result["backup_identity_matches"] is False
    assert connection.execute_calls == 0


def test_publish_reconciliation_rejects_wrong_backup_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts(
        live=(True, True),
        staging=(False, False),
        backup=(True, False),
        failed=(False, False),
    )
    importer = _OwnershipImporter([facts])
    monkeypatch.setattr(full_import_module, "_count_rows", lambda *args: 1)

    result = importer._reconcile_publish(
        build_import_table_names(str(uuid4())),
        marker="owned-marker",
        expected_rows=1,
        live_existed=True,
        previous_live_identity=TableIdentity(8, "different", "different"),
    )

    assert result["state"] == "unknown"
    assert result["backup_identity_matches"] is False


def test_staging_truncate_requires_current_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importer = FullDataImporter(
        _ContextEngine(),
        Settings(app_environment="test", mysql_database="test_import"),
    )
    monkeypatch.setattr(full_import_module, "_table_exists", lambda *args: True)
    monkeypatch.setattr(full_import_module, "_table_comment", lambda *args: "other")

    with pytest.raises(ImportPipelineError) as caught:
        importer._truncate_staging("inpatient_staging_test", "owned")

    assert caught.value.code == "STAGING_OWNERSHIP_MISMATCH"


@pytest.mark.parametrize("method_name", ["_load_with_local_infile", "_load_with_inserts"])
def test_both_load_paths_reject_changed_row_width_before_database_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    source = _accepted_source(tmp_path)
    source.csv_path.write_text(
        ",".join(CANONICAL_COLUMNS) + "\n" + ",".join([""] * 32) + "\n",
        encoding="utf-8",
    )
    importer = FullDataImporter(
        _EngineStub(),
        Settings(app_environment="test", mysql_database="test_import"),
    )
    monkeypatch.setattr(
        full_import_module.pymysql,
        "connect",
        lambda **kwargs: pytest.fail("database connection must not be opened"),
    )

    with pytest.raises(ImportPipelineError) as caught:
        if method_name == "_load_with_local_infile":
            importer._load_with_local_infile(source, "inpatient_staging_test")
        else:
            importer._load_with_inserts(
                source, "inpatient_staging_test", 100, lambda rows, batches: None
            )
    assert caught.value.code == "SOURCE_CHANGED_DURING_IMPORT"
