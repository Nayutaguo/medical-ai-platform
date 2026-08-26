"""Fail-closed full-dataset import with an ownership-aware atomic publish.

Only a complete, successful cleaning manifest can authorize publication. The
cleaned CSV is verified before and after staging, every temporary table carries
an import ownership marker, and a MySQL named lock serializes publishers. A
lost ``RENAME TABLE`` acknowledgement is reconciled from database state on a
new connection instead of being guessed or blindly rolled back.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from uuid import UUID, uuid4

import pymysql
from sqlalchemy import Column, Engine, MetaData, Table, text

from medical_ai.config import Settings
from medical_ai.data import CANONICAL_COLUMNS, cast_cleaned_row
from medical_ai.data.cleaning import SPARCS_SCHEMA_VERSION
from medical_ai.db.schema import get_table_spec, sqlalchemy_type

LIVE_TABLE = "inpatient"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 1_000_000
DEFAULT_BATCH_SIZE = 5_000
IMPORT_LOCK_TIMEOUT_SECONDS = 0
MARKER_PREFIX = "medical_ai_import:v1"

COMMON_INDEXES: Mapping[str, str] = {
    "idx_inpatient_facility_year": "(`PermanentFacilityId`(128), `DischargeYear`)",
    "idx_inpatient_year_age": "(`DischargeYear`, `AgeGroup`(32))",
    "idx_inpatient_age": "(`AgeGroup`(32))",
    "idx_inpatient_gender": "(`Gender`(32))",
    "idx_inpatient_admission": "(`AdmissionType`(64))",
    "idx_inpatient_ccsr_diagnosis": "(`CCSRDiagnosisCode`(32))",
    "idx_inpatient_facility": "(`FacilityName`(128))",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceManifestError(ValueError):
    """The cleaning manifest or its bound CSV failed verification."""

    code = "SOURCE_MANIFEST_INVALID"


class ImportPipelineError(RuntimeError):
    """Stable, non-sensitive import failure suitable for an audit artifact."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocalInfileUnavailable(ImportPipelineError):
    """The server or client disallowed ``LOAD DATA LOCAL INFILE``."""

    def __init__(self) -> None:
        super().__init__(
            "LOCAL_INFILE_UNAVAILABLE",
            "LOAD DATA LOCAL INFILE is unavailable; use the insert method or auto fallback.",
        )


@dataclass(frozen=True, slots=True)
class CleanSource:
    """A complete cleaned CSV bound to a successful immutable manifest snapshot."""

    csv_path: Path
    manifest_path: Path | None
    row_count: int
    sha256: str
    size_bytes: int
    schema_version: str
    cleaning_task_id: str | None = None
    raw_source_sha256: str | None = None
    source_complete: bool = True
    manifest_sha256: str | None = None
    mtime_ns: int | None = None
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """Bounded options for one staging import.

    ``keep_backup`` is retained as an API compatibility field, but disabling it
    is forbidden. Backup deletion is a separate, audited retention/GC concern.
    """

    task_id: str
    method: Literal["auto", "load-data", "insert"] = "auto"
    batch_size: int = DEFAULT_BATCH_SIZE
    dry_run: bool = False
    keep_backup: bool = True
    keep_failed_staging: bool = False

    def __post_init__(self) -> None:
        normalize_task_id(self.task_id)
        if self.method not in {"auto", "load-data", "insert"}:
            raise ValueError("unsupported import method")
        if not 100 <= self.batch_size <= 100_000:
            raise ValueError("batch_size must be between 100 and 100000")
        if not self.keep_backup:
            raise ValueError("backup deletion is forbidden during import; use audited retention GC")


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Non-sensitive result of a completed import attempt."""

    task_id: str
    status: Literal["validated", "succeeded"]
    method_used: str | None
    loaded_rows: int
    live_rows_before: int | None
    live_rows_after: int | None
    staging_table: str
    backup_table: str | None
    audit_path: Path


@dataclass(frozen=True, slots=True)
class ImportTableNames:
    staging: str
    backup: str
    failed: str


@dataclass(frozen=True, slots=True)
class CsvVerification:
    sha256: str
    row_count: int
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class AuditFileIdentity:
    """Identity and digest of the task-owned audit checkpoint."""

    device: int
    inode: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TableIdentity:
    """Facts that must survive a live-to-backup table rename unchanged."""

    row_count: int
    comment: str
    schema_fingerprint: str


def normalize_task_id(task_id: str) -> str:
    """Return a lowercase UUID hex token suitable for internal identifiers."""

    try:
        return UUID(str(task_id)).hex
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("task_id must be a UUID") from exc


def build_import_table_names(task_id: str) -> ImportTableNames:
    """Build validated MySQL identifiers from an application-generated UUID."""

    suffix = normalize_task_id(task_id)
    names = ImportTableNames(
        staging=f"inpatient_staging_{suffix}",
        backup=f"inpatient_backup_{suffix}",
        failed=f"inpatient_failed_{suffix}",
    )
    for name in (names.staging, names.backup, names.failed):
        _quoted_identifier(name)
    return names


def build_table_marker(task_id: str, source_sha256: str) -> str:
    """Return the compact ownership marker persisted as the MySQL table comment."""

    return (
        f"{MARKER_PREFIX};task_id={normalize_task_id(task_id)};"
        f"source_sha256={_sha256(source_sha256, 'source_sha256')}"
    )


def default_audit_path(csv_path: Path, task_id: str) -> Path:
    """Return a task-specific append-only audit path."""

    csv_path = csv_path.expanduser().resolve()
    return csv_path.with_name(
        f"{csv_path.stem}.{normalize_task_id(task_id)}.load.audit.jsonl"
    )


def sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    """Hash a file without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace a private JSON artifact already owned by this task."""

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary_path.unlink(missing_ok=True)
        raise


def _create_json_exclusive(
    path: Path, payload: Mapping[str, Any]
) -> AuditFileIdentity:
    """Create the first audit checkpoint without overwriting any existing file."""

    path = path.expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ImportPipelineError(
            "AUDIT_PATH_CONFLICT",
            "the audit path already exists; use a new task ID or a new audit path",
        ) from exc
    except OSError as exc:
        raise ImportPipelineError(
            "AUDIT_CREATE_FAILED", "the protected audit file could not be created"
        ) from exc
    try:
        _append_json_descriptor(descriptor, payload)
        identity = _audit_identity_from_descriptor(descriptor)
        _assert_path_matches_descriptor(path, descriptor)
        os.fchmod(descriptor, 0o600)
    except Exception as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        # Never unlink by pathname here: a concurrent replacement between a
        # path check and unlink could delete somebody else's file. A failed
        # task retains its private 0600 append-only fragment for investigation.
        if isinstance(exc, ImportPipelineError):
            raise
        raise ImportPipelineError(
            "AUDIT_CREATE_FAILED",
            "the protected append-only audit could not be initialized",
        ) from exc
    os.close(descriptor)
    return identity


def _audit_file_identity(path: Path) -> AuditFileIdentity:
    """Read one audit inode through a no-follow descriptor."""

    descriptor = _open_audit_descriptor(path, os.O_RDONLY)
    try:
        identity = _audit_identity_from_descriptor(descriptor)
        _assert_path_matches_descriptor(path, descriptor)
        return identity
    finally:
        os.close(descriptor)


def _guarded_write_json(
    path: Path,
    payload: Mapping[str, Any],
    expected: AuditFileIdentity,
) -> AuditFileIdentity:
    """Update only the exact audit inode opened without following links.

    Writing through the already-open descriptor means a concurrent path swap
    can never redirect the checkpoint into the replacement file or a symlink
    target. A post-write path check then fails closed if the owned inode is no
    longer reachable at the task-specific audit path.
    """

    try:
        descriptor = _open_audit_descriptor(path, os.O_RDWR | os.O_APPEND)
    except (OSError, ImportPipelineError) as exc:
        raise ImportPipelineError(
            "AUDIT_OWNERSHIP_LOST",
            "the protected audit checkpoint is missing or was replaced",
        ) from exc
    try:
        current = _audit_identity_from_descriptor(descriptor)
        if current != expected:
            raise ImportPipelineError(
                "AUDIT_OWNERSHIP_LOST",
                "the protected audit checkpoint changed outside this import task",
            )
        _append_json_descriptor(descriptor, payload)
        updated = _audit_identity_from_descriptor(descriptor)
        _assert_path_matches_descriptor(path, descriptor)
        return updated
    except ImportPipelineError:
        raise
    except OSError as exc:
        raise ImportPipelineError(
            "AUDIT_PERSIST_FAILED",
            "the protected import audit checkpoint could not be persisted",
        ) from exc
    finally:
        os.close(descriptor)


def _open_audit_descriptor(path: Path, access_flags: int) -> int:
    """Open an audit path without resolving or following its final component."""

    flags = access_flags
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(descriptor)
        raise ImportPipelineError(
            "AUDIT_OWNERSHIP_LOST",
            "the protected audit path is no longer a regular file",
        )
    return descriptor


def _audit_identity_from_descriptor(descriptor: int) -> AuditFileIdentity:
    """Hash and identify the same open audit inode, rejecting in-place races."""

    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ImportPipelineError(
            "AUDIT_OWNERSHIP_LOST",
            "the protected audit descriptor is no longer a regular file",
        )
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(descriptor)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise ImportPipelineError(
            "AUDIT_OWNERSHIP_LOST",
            "the protected audit checkpoint changed while it was being read",
        )
    return AuditFileIdentity(
        device=after.st_dev,
        inode=after.st_ino,
        size_bytes=after.st_size,
        sha256=digest.hexdigest(),
    )


def _assert_path_matches_descriptor(path: Path, descriptor: int) -> None:
    """Require the audit pathname to still name the open owned inode."""

    descriptor_stat = os.fstat(descriptor)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise ImportPipelineError(
            "AUDIT_OWNERSHIP_LOST",
            "the protected audit checkpoint path is no longer owned by this task",
        ) from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino)
        != (descriptor_stat.st_dev, descriptor_stat.st_ino)
    ):
        raise ImportPipelineError(
            "AUDIT_OWNERSHIP_LOST",
            "the protected audit checkpoint path was replaced",
        )


def _append_json_descriptor(descriptor: int, payload: Mapping[str, Any]) -> None:
    """Append and fsync one full audit snapshot as a JSON Lines record.

    A crash can leave only the newest line incomplete; every earlier newline-
    terminated checkpoint remains intact and recoverable.
    """

    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise OSError("audit descriptor write made no progress")
        offset += written
    os.fsync(descriptor)


def load_clean_source(
    csv_path: Path,
    *,
    manifest_path: Path | None = None,
    expected_rows: int | None = None,
    expected_sha256: str | None = None,
    allow_partial_source: bool = False,
) -> CleanSource:
    """Verify a complete cleaned CSV against its successful cleaning manifest.

    Legacy override arguments remain only to fail closed for older callers.
    They cannot authorize a product import.
    """

    if expected_rows is not None or expected_sha256 is not None:
        raise SourceManifestError("explicit row/hash acceptance is disabled; use the manifest")
    if allow_partial_source:
        raise SourceManifestError("partial-source publication is disabled")
    if manifest_path is None:
        raise SourceManifestError("a successful cleaning manifest is required")

    csv_path = csv_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if csv_path == manifest_path:
        raise SourceManifestError("cleaned CSV and manifest paths must be different")
    if not csv_path.is_file():
        raise SourceManifestError("cleaned CSV does not exist")
    if not manifest_path.is_file():
        raise SourceManifestError("cleaning manifest does not exist")

    manifest, manifest_sha256 = _read_manifest_snapshot(manifest_path)
    facts = _manifest_facts(manifest, csv_path)
    verification = _scan_canonical_csv(csv_path)
    if verification.row_count != facts["row_count"]:
        raise SourceManifestError("cleaned CSV row count does not match the manifest")
    if verification.sha256 != facts["sha256"]:
        raise SourceManifestError("cleaned CSV SHA-256 does not match the manifest")

    return CleanSource(
        csv_path=csv_path,
        manifest_path=manifest_path,
        row_count=facts["row_count"],
        sha256=verification.sha256,
        size_bytes=verification.size_bytes,
        schema_version=SPARCS_SCHEMA_VERSION,
        cleaning_task_id=facts["cleaning_task_id"],
        raw_source_sha256=facts["raw_source_sha256"],
        source_complete=True,
        manifest_sha256=manifest_sha256,
        mtime_ns=verification.mtime_ns,
        device=verification.device,
        inode=verification.inode,
    )


class FullDataImporter:
    """Load one verified complete dataset and atomically publish it."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def run(
        self,
        source: CleanSource,
        options: ImportOptions,
        *,
        audit_path: Path,
        progress: Callable[[int], None] | None = None,
    ) -> ImportResult:
        """Execute the ownership-aware import state machine."""

        names = build_import_table_names(options.task_id)
        task_id = str(UUID(options.task_id))
        marker = build_table_marker(options.task_id, source.sha256)
        audit_path = self._validate_audit_path(
            source, audit_path, task_id=options.task_id
        )
        audit: dict[str, Any] = {
            "task_id": task_id,
            "audit_format": "jsonl-full-snapshots-v1",
            "status": "validating",
            "schema_version": source.schema_version,
            "source": {
                "file_name": source.csv_path.name,
                "size_bytes": source.size_bytes,
                "sha256": source.sha256,
                "row_count": source.row_count,
                "cleaning_task_id": source.cleaning_task_id,
                "raw_source_sha256": source.raw_source_sha256,
                "manifest_file_name": source.manifest_path.name
                if source.manifest_path
                else None,
                "manifest_sha256": source.manifest_sha256,
            },
            "options": {
                "requested_method": options.method,
                "batch_size": options.batch_size,
                "dry_run": options.dry_run,
                "backup_policy": "retain_until_separate_audited_gc",
                "restart_strategy": "new_isolated_staging_table",
            },
            "tables": {
                "live": LIVE_TABLE,
                "staging": names.staging,
                "backup": names.backup,
                "failed": names.failed,
            },
            "ownership_marker": marker,
            "lock": {
                "name": self._lock_name(),
                "acquired": False,
                "released": False,
                "release_error_code": None,
            },
            "cleanup": {
                "staging": {
                    "attempted": False,
                    "status": "not_needed",
                    "error_code": None,
                },
                "source_snapshot": {
                    "attempted": False,
                    "status": "not_created",
                    "error_code": None,
                },
            },
            "publish_reconciliation": None,
            "restoration_reconciliation": None,
            "quarantine_reconciliation": None,
            "started_at": _utc_now(),
            "finished_at": None,
            "method_used": None,
            "fallback_code": None,
            "attempted_rows": source.row_count,
            "loaded_rows": 0,
            "rejected_rows": 0,
            "batches_committed": 0,
            "live_rows_before": None,
            "live_rows_after": None,
            "staging_rows": None,
            "published": False,
            "backup_retained": False,
            "error": None,
        }
        audit_identity = _create_json_exclusive(audit_path, audit)

        lock_connection: Any | None = None
        lock_acquired = False
        live_existed = False
        staging_created = False
        staging_cleanup_allowed = False
        snapshot_source: CleanSource | None = None
        result: ImportResult | None = None
        failure: ImportPipelineError | None = None

        try:
            self._verify_manifest_binding(source)
            lock_connection = self.engine.connect()
            self._acquire_import_lock(lock_connection)
            lock_acquired = True
            audit["lock"]["acquired"] = True

            live_existed, previous_live_identity = self._inspect_initial_state(
                lock_connection, names
            )
            live_rows_before = (
                previous_live_identity.row_count
                if previous_live_identity is not None
                else None
            )
            audit["live_rows_before"] = live_rows_before
            audit["previous_live_identity"] = (
                {
                    "row_count": previous_live_identity.row_count,
                    "comment_sha256": hashlib.sha256(
                        previous_live_identity.comment.encode("utf-8")
                    ).hexdigest(),
                    "schema_fingerprint": previous_live_identity.schema_fingerprint,
                }
                if previous_live_identity is not None
                else None
            )

            if options.dry_run:
                self._verify_source_snapshot(source)
                result = ImportResult(
                    task_id=task_id,
                    status="validated",
                    method_used=None,
                    loaded_rows=0,
                    live_rows_before=live_rows_before,
                    live_rows_after=None,
                    staging_table=names.staging,
                    backup_table=None,
                    audit_path=audit_path,
                )
            else:
                snapshot_source = self._create_source_snapshot(
                    source, options.task_id
                )
                audit["cleanup"]["source_snapshot"]["status"] = "active"
                audit["source_snapshot"] = {
                    "name": snapshot_source.csv_path.name,
                    "sha256": snapshot_source.sha256,
                }
                staging_created = True
                staging_cleanup_allowed = True
                self._create_staging(names.staging, marker=marker)
                self._assert_owned_table(names.staging, marker)
                audit["status"] = "loading"
                audit_identity = self._write_checkpoint(
                    audit_path, audit, audit_identity
                )

                def checkpoint(loaded_rows: int, batches: int) -> None:
                    nonlocal audit_identity
                    audit["loaded_rows"] = loaded_rows
                    audit["batches_committed"] = batches
                    audit_identity = self._write_checkpoint(
                        audit_path, audit, audit_identity
                    )
                    if progress is not None:
                        progress(loaded_rows)

                method_used, loaded_rows = self._load_source(
                    snapshot_source, names.staging, options, checkpoint, audit
                )
                audit["method_used"] = method_used
                self._verify_source_snapshot(snapshot_source)

                audit["status"] = "validating"
                staging_rows = self._validate_staging(
                    names.staging, marker, source.row_count, loaded_rows
                )
                audit["staging_rows"] = staging_rows
                self._ensure_indexes(names.staging)
                self._assert_publish_preconditions(
                    names,
                    marker=marker,
                    expected_rows=source.row_count,
                    live_existed=live_existed,
                    previous_live_identity=previous_live_identity,
                )
                self._assert_import_lock_owned(lock_connection)

                audit["status"] = "publishing"
                audit_identity = self._write_checkpoint(
                    audit_path, audit, audit_identity
                )
                publish_acknowledged = True
                try:
                    self._publish(
                        names,
                        live_existed=live_existed,
                        connection=lock_connection,
                    )
                except Exception:
                    publish_acknowledged = False

                try:
                    reconciliation = self._reconcile_publish(
                        names,
                        marker=marker,
                        expected_rows=source.row_count,
                        live_existed=live_existed,
                        previous_live_identity=previous_live_identity,
                    )
                except Exception as exc:
                    staging_cleanup_allowed = False
                    audit["published"] = None
                    audit["publish_reconciliation"] = {
                        "state": "unknown",
                        "rename_acknowledged": publish_acknowledged,
                        "error_code": "PUBLISH_RECONCILIATION_FAILED",
                    }
                    raise ImportPipelineError(
                        "PUBLISH_STATE_UNKNOWN",
                        "publish state could not be reconciled; no table was dropped or blindly restored",
                    ) from exc
                reconciliation["rename_acknowledged"] = publish_acknowledged
                audit["publish_reconciliation"] = reconciliation
                state = reconciliation["state"]

                if state == "published":
                    staging_created = False
                    staging_cleanup_allowed = False
                    live_rows_after = int(reconciliation["live_rows"])
                    audit.update(
                        {
                            "loaded_rows": source.row_count,
                            "live_rows_after": live_rows_after,
                            "published": True,
                            "backup_retained": live_existed,
                        }
                    )
                    result = ImportResult(
                        task_id=task_id,
                        status="succeeded",
                        method_used=method_used,
                        loaded_rows=source.row_count,
                        live_rows_before=live_rows_before,
                        live_rows_after=live_rows_after,
                        staging_table=names.staging,
                        backup_table=names.backup if live_existed else None,
                        audit_path=audit_path,
                    )
                elif state == "published_invalid" and live_existed:
                    staging_created = False
                    staging_cleanup_allowed = False
                    try:
                        restoration = self._restore_previous_live(
                            names,
                            marker=marker,
                            previous_live_identity=previous_live_identity,
                            connection=lock_connection,
                        )
                    except Exception as exc:
                        audit["published"] = None
                        raise ImportPipelineError(
                            "PUBLISH_STATE_UNKNOWN",
                            "restore state could not be reconciled; no table was dropped or blindly restored",
                        ) from exc
                    audit["restoration_reconciliation"] = restoration
                    if restoration["state"] == "restored":
                        audit["published"] = False
                        raise ImportPipelineError(
                            "POST_PUBLISH_VERIFICATION_FAILED_RESTORED",
                            "published row verification failed; the owned table was preserved and the previous live table was restored",
                        )
                    raise ImportPipelineError(
                        "PUBLISH_STATE_UNKNOWN",
                        "publish verification is indeterminate; no table was dropped or blindly restored",
                    )
                elif state == "published_invalid":
                    staging_created = False
                    staging_cleanup_allowed = False
                    try:
                        quarantine = self._quarantine_failed_first_publish(
                            names,
                            marker=marker,
                            connection=lock_connection,
                        )
                    except Exception as exc:
                        audit["published"] = None
                        raise ImportPipelineError(
                            "PUBLISH_STATE_UNKNOWN",
                            "first-publish quarantine could not be reconciled; no table was dropped",
                        ) from exc
                    audit["quarantine_reconciliation"] = quarantine
                    if quarantine["state"] == "quarantined":
                        audit["published"] = False
                        raise ImportPipelineError(
                            "POST_PUBLISH_VERIFICATION_FAILED_QUARANTINED",
                            "first-publish row verification failed; the owned table was isolated for investigation",
                        )
                    audit["published"] = None
                    raise ImportPipelineError(
                        "PUBLISH_STATE_UNKNOWN",
                        "first-publish quarantine is indeterminate; no table was dropped",
                    )
                elif state == "not_published":
                    raise ImportPipelineError(
                        "PUBLISH_NOT_APPLIED",
                        "the atomic publish was not applied; the live table was not replaced",
                    )
                else:
                    staging_cleanup_allowed = False
                    audit["published"] = None
                    raise ImportPipelineError(
                        "PUBLISH_STATE_UNKNOWN",
                        "publish state is indeterminate; no table was dropped or blindly restored",
                    )
        except ImportPipelineError as exc:
            failure = exc
        except SourceManifestError as exc:
            failure = ImportPipelineError(exc.code, str(exc))
        except Exception as exc:
            failure = ImportPipelineError(
                "DATABASE_OPERATION_FAILED",
                "database import operation failed; inspect protected server logs",
            )
            failure.__cause__ = exc
        finally:
            if failure is not None and staging_created:
                cleanup = audit["cleanup"]["staging"]
                if options.keep_failed_staging:
                    cleanup["status"] = "retained_by_request"
                elif not staging_cleanup_allowed:
                    cleanup["status"] = "skipped_unknown_publish_state"
                else:
                    cleanup["attempted"] = True
                    try:
                        cleanup["status"] = self._cleanup_owned_staging(
                            names.staging, marker
                        )
                    except ImportPipelineError as cleanup_exc:
                        cleanup.update(
                            {"status": "failed", "error_code": cleanup_exc.code}
                        )
                    except Exception:
                        cleanup.update(
                            {
                                "status": "failed",
                                "error_code": "STAGING_CLEANUP_FAILED",
                            }
                        )

            if snapshot_source is not None:
                snapshot_cleanup = audit["cleanup"]["source_snapshot"]
                retain_snapshot = (
                    failure is not None
                    and (
                        failure.code == "PUBLISH_STATE_UNKNOWN"
                        or options.keep_failed_staging
                    )
                )
                if retain_snapshot:
                    snapshot_cleanup["status"] = "retained_for_reconciliation"
                else:
                    snapshot_cleanup["attempted"] = True
                    try:
                        snapshot_cleanup["status"] = self._cleanup_source_snapshot(
                            snapshot_source
                        )
                    except ImportPipelineError as cleanup_exc:
                        snapshot_cleanup.update(
                            {"status": "failed", "error_code": cleanup_exc.code}
                        )
                    except Exception:
                        snapshot_cleanup.update(
                            {
                                "status": "failed",
                                "error_code": "SOURCE_SNAPSHOT_CLEANUP_FAILED",
                            }
                        )

            if lock_connection is not None and lock_acquired:
                try:
                    self._release_import_lock(lock_connection)
                    audit["lock"]["released"] = True
                except Exception:
                    audit["lock"]["release_error_code"] = "IMPORT_LOCK_RELEASE_FAILED"
            if lock_connection is not None:
                try:
                    lock_connection.close()
                except Exception:
                    if audit["lock"]["release_error_code"] is None:
                        audit["lock"]["release_error_code"] = "IMPORT_LOCK_CONNECTION_CLOSE_FAILED"

        if failure is None and result is None:
            failure = ImportPipelineError(
                "IMPORT_STATE_INVALID", "the importer did not reach a terminal state"
            )

        if failure is None:
            audit.update(
                {
                    "status": "validated" if result and result.status == "validated" else "succeeded",
                    "finished_at": _utc_now(),
                    "error": None,
                }
            )
        else:
            audit.update(
                {
                    "status": "unknown"
                    if failure.code == "PUBLISH_STATE_UNKNOWN"
                    else "failed",
                    "finished_at": _utc_now(),
                    "error": {"code": failure.code, "message": str(failure)},
                }
            )

        try:
            audit_identity = self._write_checkpoint(
                audit_path, audit, audit_identity
            )
        except ImportPipelineError as exc:
            if failure is not None:
                raise failure from exc
            raise
        except Exception as exc:
            if failure is not None:
                raise failure from exc
            raise ImportPipelineError(
                "AUDIT_PERSIST_FAILED",
                "the protected final audit could not be persisted; backup tables were retained",
            ) from exc

        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _validate_audit_path(
        self, source: CleanSource, audit_path: Path, *, task_id: str
    ) -> Path:
        resolved = audit_path.expanduser().resolve()
        protected = {
            source.csv_path.expanduser().resolve(),
            source.csv_path.with_suffix(".profile.json").expanduser().resolve(),
            source.csv_path.with_name(
                f".{source.csv_path.name}.{normalize_task_id(task_id)}.import-snapshot"
            ).resolve(),
        }
        if source.manifest_path is not None:
            protected.add(source.manifest_path.expanduser().resolve())
        if resolved in protected:
            raise ImportPipelineError(
                "AUDIT_PATH_INVALID",
                "audit, cleaned CSV, profile, and manifest paths must be different",
            )
        return resolved

    def _write_checkpoint(
        self,
        audit_path: Path,
        audit: Mapping[str, Any],
        expected_identity: AuditFileIdentity,
    ) -> AuditFileIdentity:
        try:
            return _guarded_write_json(audit_path, audit, expected_identity)
        except ImportPipelineError:
            raise
        except Exception as exc:
            raise ImportPipelineError(
                "AUDIT_PERSIST_FAILED", "a protected import audit checkpoint could not be persisted"
            ) from exc

    def _verify_manifest_binding(self, source: CleanSource) -> None:
        if (
            source.manifest_path is None
            or source.manifest_sha256 is None
            or not source.source_complete
        ):
            raise SourceManifestError("a complete successful cleaning manifest is required")
        manifest_path = source.manifest_path.expanduser().resolve()
        try:
            manifest, manifest_digest = _read_manifest_snapshot(manifest_path)
        except SourceManifestError:
            raise
        except OSError as exc:
            raise SourceManifestError(
                "cleaning manifest is unavailable after source acceptance"
            ) from exc
        if manifest_digest != source.manifest_sha256:
            raise SourceManifestError("cleaning manifest changed after source acceptance")
        facts = _manifest_facts(manifest, source.csv_path.resolve())
        if (
            facts["row_count"] != source.row_count
            or facts["sha256"] != source.sha256
            or facts["cleaning_task_id"] != source.cleaning_task_id
            or facts["raw_source_sha256"] != source.raw_source_sha256
            or facts["schema_version"] != source.schema_version
        ):
            raise SourceManifestError("cleaning manifest no longer matches the accepted source")

    def _verify_source_snapshot(self, source: CleanSource) -> CsvVerification:
        try:
            verification = _scan_canonical_csv(source.csv_path)
        except SourceManifestError as exc:
            raise ImportPipelineError(
                "SOURCE_CHANGED_DURING_IMPORT",
                "the cleaned CSV failed strict verification during import",
            ) from exc
        accepted_identity = (
            source.size_bytes,
            source.mtime_ns,
            source.device,
            source.inode,
        )
        current_identity = (
            verification.size_bytes,
            verification.mtime_ns,
            verification.device,
            verification.inode,
        )
        if None in accepted_identity or current_identity != accepted_identity:
            raise ImportPipelineError(
                "SOURCE_CHANGED_DURING_IMPORT",
                "the cleaned CSV file identity changed after manifest acceptance",
            )
        if verification.sha256 != source.sha256:
            raise ImportPipelineError(
                "SOURCE_CHANGED_DURING_IMPORT", "the cleaned CSV hash changed during import"
            )
        if verification.row_count != source.row_count:
            raise ImportPipelineError(
                "SOURCE_CHANGED_DURING_IMPORT", "the cleaned CSV row count changed during import"
            )
        return verification

    def _create_source_snapshot(
        self, source: CleanSource, task_id: str
    ) -> CleanSource:
        """Bind loading to a task-private hard-link snapshot of accepted bytes."""

        suffix = normalize_task_id(task_id)
        snapshot_path = source.csv_path.with_name(
            f".{source.csv_path.name}.{suffix}.import-snapshot"
        )
        try:
            os.link(source.csv_path, snapshot_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ImportPipelineError(
                "SOURCE_SNAPSHOT_CONFLICT",
                "the task-specific source snapshot already exists; use a new task ID",
            ) from exc
        except OSError as exc:
            raise ImportPipelineError(
                "SOURCE_SNAPSHOT_CREATE_FAILED",
                "a stable source snapshot could not be created beside the cleaned CSV",
            ) from exc

        snapshot_source = replace(source, csv_path=snapshot_path)
        try:
            self._verify_source_snapshot(snapshot_source)
        except Exception:
            try:
                current = snapshot_path.lstat()
                if (
                    stat.S_ISREG(current.st_mode)
                    and current.st_dev == source.device
                    and current.st_ino == source.inode
                ):
                    snapshot_path.unlink()
            except OSError:
                pass
            raise
        return snapshot_source

    def _cleanup_source_snapshot(self, source: CleanSource) -> str:
        """Unlink only the exact task-private source snapshot we created."""

        try:
            current = source.csv_path.lstat()
        except FileNotFoundError:
            return "already_absent"
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != source.device
            or current.st_ino != source.inode
        ):
            raise ImportPipelineError(
                "SOURCE_SNAPSHOT_OWNERSHIP_MISMATCH",
                "the source snapshot path changed and was retained",
            )
        source.csv_path.unlink()
        return "removed_owned_snapshot"

    def _lock_name(self) -> str:
        database_digest = hashlib.sha256(
            self.settings.mysql_database.encode("utf-8")
        ).hexdigest()[:16]
        return f"medical-ai:full-import:{database_digest}"

    def _acquire_import_lock(self, connection: Any) -> None:
        result = connection.execute(
            text("SELECT GET_LOCK(:lock_name, :timeout_seconds)"),
            {
                "lock_name": self._lock_name(),
                "timeout_seconds": IMPORT_LOCK_TIMEOUT_SECONDS,
            },
        ).scalar_one_or_none()
        if result == 0:
            raise ImportPipelineError(
                "IMPORT_LOCK_BUSY", "another full-data publisher currently holds the import lock"
            )
        if result != 1:
            raise ImportPipelineError(
                "IMPORT_LOCK_FAILED", "the database import lock could not be acquired"
            )

    def _release_import_lock(self, connection: Any) -> None:
        result = connection.execute(
            text("SELECT RELEASE_LOCK(:lock_name)"),
            {"lock_name": self._lock_name()},
        ).scalar_one_or_none()
        if result != 1:
            raise ImportPipelineError(
                "IMPORT_LOCK_RELEASE_FAILED", "the database import lock release was not acknowledged"
            )

    def _assert_import_lock_owned(self, connection: Any) -> None:
        """Fail closed unless the original session still owns the publish lock."""

        try:
            owned = connection.execute(
                text(
                    "SELECT CASE WHEN IS_USED_LOCK(:lock_name) = CONNECTION_ID() "
                    "THEN 1 ELSE 0 END"
                ),
                {"lock_name": self._lock_name()},
            ).scalar_one_or_none()
        except Exception as exc:
            raise ImportPipelineError(
                "IMPORT_LOCK_LOST",
                "the database import lock could not be verified before publish",
            ) from exc
        if owned != 1:
            raise ImportPipelineError(
                "IMPORT_LOCK_LOST",
                "the database import lock is no longer owned by this import session",
            )

    def _inspect_initial_state(
        self, connection: Any, names: ImportTableNames
    ) -> tuple[bool, TableIdentity | None]:
        conflicts = [
            name
            for name in (names.staging, names.backup, names.failed)
            if _table_exists(connection, self.settings.mysql_database, name)
        ]
        if conflicts:
            raise ImportPipelineError(
                "IMPORT_TABLE_CONFLICT",
                "one or more task-specific staging, backup, or failed tables already exist; use a new task ID",
            )
        live_existed = _table_exists(
            connection, self.settings.mysql_database, LIVE_TABLE
        )
        return (
            live_existed,
            self._table_identity(connection, LIVE_TABLE) if live_existed else None,
        )

    def _create_staging(self, table_name: str, *, marker: str) -> None:
        """Create a canonical staging table whose first DDL owns it.

        The marker is emitted in the same ``CREATE TABLE`` statement. If the
        server creates the table but the client loses the acknowledgement,
        cleanup can therefore still prove ownership instead of leaving an
        unmarked orphan copied from a previous live table.
        """

        table = _staging_table(table_name, marker=marker)
        table.metadata.create_all(self.engine, tables=[table], checkfirst=False)

    def _assert_owned_table(self, table_name: str, marker: str) -> None:
        with self.engine.connect() as connection:
            if not _table_exists(connection, self.settings.mysql_database, table_name):
                raise ImportPipelineError(
                    "STAGING_TABLE_MISSING", "the created staging table is missing"
                )
            if _table_comment(
                connection, self.settings.mysql_database, table_name
            ) != marker:
                raise ImportPipelineError(
                    "STAGING_OWNERSHIP_MISMATCH",
                    "the staging table ownership marker does not match this task",
                )

    def _load_source(
        self,
        source: CleanSource,
        table_name: str,
        options: ImportOptions,
        checkpoint: Callable[[int, int], None],
        audit: dict[str, Any],
    ) -> tuple[str, int]:
        if options.method in {"auto", "load-data"}:
            try:
                loaded_rows = self._load_with_local_infile(source, table_name)
                checkpoint(loaded_rows, 1)
                return "load-data", loaded_rows
            except LocalInfileUnavailable:
                if options.method == "load-data":
                    raise
                audit["fallback_code"] = "LOCAL_INFILE_UNAVAILABLE"
                self._truncate_staging(
                    table_name,
                    build_table_marker(options.task_id, source.sha256),
                )
                return "insert", self._load_with_inserts(
                    source, table_name, options.batch_size, checkpoint
                )
        return "insert", self._load_with_inserts(
            source, table_name, options.batch_size, checkpoint
        )

    def _load_with_local_infile(self, source: CleanSource, table_name: str) -> int:
        # LOAD DATA does not enforce exact CSV width, so verify it ourselves.
        self._verify_source_snapshot(source)
        connection = pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_database,
            charset="utf8mb4",
            local_infile=True,
            autocommit=False,
        )
        variables = ", ".join(f"@{column}" for column in CANONICAL_COLUMNS)
        assignments = ",\n  ".join(
            f"`{column}` = NULLIF(@{column}, '')" for column in CANONICAL_COLUMNS
        )
        sql = f"""
LOAD DATA LOCAL INFILE %s
INTO TABLE {_quoted_identifier(table_name)}
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\\n'
IGNORE 1 LINES
({variables})
SET
  {assignments}
"""
        try:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(sql, (str(source.csv_path),))
                except Exception as exc:
                    connection.rollback()
                    if _is_local_infile_unavailable(exc):
                        raise LocalInfileUnavailable() from exc
                    raise
                loaded_rows = max(int(cursor.rowcount), 0)
                cursor.execute("SHOW COUNT(*) WARNINGS")
                warning_row = cursor.fetchone()
                warning_count = int(warning_row[0]) if warning_row else 0
                if warning_count:
                    connection.rollback()
                    raise ImportPipelineError(
                        "LOAD_DATA_WARNINGS",
                        "LOAD DATA reported conversion warnings; staging was rejected",
                    )
            connection.commit()
            return loaded_rows
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load_with_inserts(
        self,
        source: CleanSource,
        table_name: str,
        batch_size: int,
        checkpoint: Callable[[int, int], None],
    ) -> int:
        # Recheck before loading, then repeat exact width validation while reading.
        self._verify_source_snapshot(source)
        table = _staging_table(table_name)
        loaded_rows = 0
        committed_batches = 0
        batch: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal loaded_rows, committed_batches
            if not batch:
                return
            with self.engine.begin() as connection:
                connection.execute(table.insert(), batch)
            loaded_rows += len(batch)
            committed_batches += 1
            batch.clear()
            checkpoint(loaded_rows, committed_batches)

        try:
            with source.csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                try:
                    header = next(reader)
                except StopIteration as exc:
                    raise ImportPipelineError(
                        "CLEANED_SCHEMA_MISMATCH", "cleaned CSV has no header"
                    ) from exc
                if header != list(CANONICAL_COLUMNS):
                    raise ImportPipelineError(
                        "CLEANED_SCHEMA_MISMATCH",
                        "cleaned CSV columns do not match the canonical schema",
                    )
                for values in reader:
                    if len(values) != len(CANONICAL_COLUMNS):
                        raise ImportPipelineError(
                            "CLEANED_ROW_WIDTH_MISMATCH",
                            "cleaned CSV contains a row with a non-canonical field count",
                        )
                    row = dict(zip(CANONICAL_COLUMNS, values, strict=True))
                    try:
                        batch.append(cast_cleaned_row(row))
                    except (TypeError, ValueError, ArithmeticError) as exc:
                        raise ImportPipelineError(
                            "CLEANED_ROW_INVALID",
                            "cleaned CSV contains a value that cannot be loaded",
                        ) from exc
                    if len(batch) >= batch_size:
                        flush()
                flush()
        except UnicodeError as exc:
            raise ImportPipelineError(
                "CLEANED_CSV_ENCODING_INVALID", "cleaned CSV is not valid UTF-8"
            ) from exc
        return loaded_rows

    def _truncate_staging(self, table_name: str, marker: str) -> None:
        with self.engine.begin() as connection:
            if not _table_exists(
                connection, self.settings.mysql_database, table_name
            ) or _table_comment(
                connection, self.settings.mysql_database, table_name
            ) != marker:
                raise ImportPipelineError(
                    "STAGING_OWNERSHIP_MISMATCH",
                    "staging ownership changed before fallback reset",
                )
            connection.execute(text(f"TRUNCATE TABLE {_quoted_identifier(table_name)}"))

    def _validate_staging(
        self, table_name: str, marker: str, expected_rows: int, loaded_rows: int
    ) -> int:
        with self.engine.connect() as connection:
            if _table_comment(
                connection, self.settings.mysql_database, table_name
            ) != marker:
                raise ImportPipelineError(
                    "STAGING_OWNERSHIP_MISMATCH",
                    "the staging table ownership marker changed before validation",
                )
            staging_rows = _count_rows(connection, table_name)
            invalid_birth_weight_rows = int(
                connection.execute(
                    text(
                        f"SELECT COUNT(*) FROM {_quoted_identifier(table_name)} "
                        "WHERE `BirthWeight` IS NOT NULL "
                        "AND COALESCE(`AdmissionType`, '') <> 'Newborn'"
                    )
                ).scalar_one()
            )
        if loaded_rows != expected_rows or staging_rows != expected_rows:
            raise ImportPipelineError(
                "ROW_COUNT_MISMATCH",
                "staging row count does not match the accepted cleaning manifest",
            )
        if invalid_birth_weight_rows:
            raise ImportPipelineError(
                "BIRTH_WEIGHT_RULE_VIOLATION",
                "staging data violates the newborn birth-weight rule",
            )
        return staging_rows

    def _ensure_indexes(self, table_name: str) -> None:
        with self.engine.begin() as connection:
            existing = {
                str(row[0])
                for row in connection.execute(
                    text(
                        """
                        SELECT DISTINCT INDEX_NAME
                        FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA = :schema_name
                          AND TABLE_NAME = :table_name
                        """
                    ),
                    {"schema_name": self.settings.mysql_database, "table_name": table_name},
                )
            }
            for index_name, columns in COMMON_INDEXES.items():
                if index_name not in existing:
                    connection.execute(
                        text(
                            f"CREATE INDEX {_quoted_identifier(index_name)} "
                            f"ON {_quoted_identifier(table_name)} {columns}"
                        )
                    )

    def _assert_publish_preconditions(
        self,
        names: ImportTableNames,
        *,
        marker: str,
        expected_rows: int,
        live_existed: bool,
        previous_live_identity: TableIdentity | None,
    ) -> None:
        with self.engine.connect() as connection:
            if any(
                _table_exists(connection, self.settings.mysql_database, table_name)
                for table_name in (names.backup, names.failed)
            ):
                raise ImportPipelineError(
                    "IMPORT_TABLE_CONFLICT",
                    "a task-specific backup or failed table appeared before publish",
                )
            if not _table_exists(
                connection, self.settings.mysql_database, names.staging
            ) or _table_comment(
                connection, self.settings.mysql_database, names.staging
            ) != marker:
                raise ImportPipelineError(
                    "STAGING_OWNERSHIP_MISMATCH",
                    "staging ownership could not be verified immediately before publish",
                )
            if _count_rows(connection, names.staging) != expected_rows:
                raise ImportPipelineError(
                    "ROW_COUNT_MISMATCH", "staging rows changed before publish"
                )
            current_live_exists = _table_exists(
                connection, self.settings.mysql_database, LIVE_TABLE
            )
            if current_live_exists != live_existed:
                raise ImportPipelineError(
                    "LIVE_TABLE_STATE_CHANGED", "live table existence changed before publish"
                )
            if live_existed and (
                previous_live_identity is None
                or self._table_identity(connection, LIVE_TABLE)
                != previous_live_identity
            ):
                raise ImportPipelineError(
                    "LIVE_TABLE_STATE_CHANGED",
                    "live table identity changed before publish",
                )

    def _publish(
        self,
        names: ImportTableNames,
        *,
        live_existed: bool,
        connection: Any,
    ) -> None:
        """Publish on the same server session that owns the named lock."""

        if live_existed:
            connection.execute(
                text(
                    "RENAME TABLE "
                    f"{_quoted_identifier(LIVE_TABLE)} TO {_quoted_identifier(names.backup)}, "
                    f"{_quoted_identifier(names.staging)} TO {_quoted_identifier(LIVE_TABLE)}"
                )
            )
        else:
            connection.execute(
                text(
                    f"RENAME TABLE {_quoted_identifier(names.staging)} "
                    f"TO {_quoted_identifier(LIVE_TABLE)}"
                )
            )

    def _reconcile_publish(
        self,
        names: ImportTableNames,
        *,
        marker: str,
        expected_rows: int,
        live_existed: bool,
        previous_live_identity: TableIdentity | None,
    ) -> dict[str, Any]:
        # Reconciliation never trusts the possibly broken RENAME session.
        with self.engine.connect() as connection:
            facts = self._table_facts(connection, names, marker)
            live_rows = (
                _count_rows(connection, LIVE_TABLE)
                if facts["live"]["exists"] and facts["live"]["owned"]
                else None
            )
            backup_identity_matches = (
                self._table_identity(connection, names.backup)
                == previous_live_identity
                if live_existed
                and previous_live_identity is not None
                and facts["backup"]["exists"]
                else not live_existed and not facts["backup"]["exists"]
            )
            previous_live_still_matches = (
                self._table_identity(connection, LIVE_TABLE)
                == previous_live_identity
                if live_existed
                and previous_live_identity is not None
                and facts["live"]["exists"]
                and not facts["live"]["owned"]
                else not live_existed and not facts["live"]["exists"]
            )

        published_shape = (
            facts["live"]["owned"]
            and not facts["staging"]["exists"]
            and not facts["failed"]["exists"]
            and facts["backup"]["exists"] == live_existed
            and backup_identity_matches
        )
        not_published_shape = (
            facts["staging"]["owned"]
            and not facts["backup"]["exists"]
            and not facts["failed"]["exists"]
            and previous_live_still_matches
        )
        if published_shape and live_rows == expected_rows:
            state = "published"
        elif published_shape:
            state = "published_invalid"
        elif not_published_shape:
            state = "not_published"
        else:
            state = "unknown"
        return {
            "state": state,
            "live_rows": live_rows,
            "tables": facts,
            "backup_identity_matches": backup_identity_matches,
            "previous_live_identity_matches": previous_live_still_matches,
        }

    def _restore_previous_live(
        self,
        names: ImportTableNames,
        *,
        marker: str,
        previous_live_identity: TableIdentity | None,
        connection: Any,
    ) -> dict[str, Any]:
        # Recovery requires proof that the current live table belongs to us.
        with self.engine.connect() as inspection_connection:
            before = self._table_facts(inspection_connection, names, marker)
            backup_identity_matches = (
                previous_live_identity is not None
                and before["backup"]["exists"]
                and self._table_identity(inspection_connection, names.backup)
                == previous_live_identity
            )
        authorized = (
            before["live"]["owned"]
            and not before["staging"]["exists"]
            and before["backup"]["exists"]
            and not before["failed"]["exists"]
            and backup_identity_matches
        )
        if not authorized:
            return {
                "state": "unknown",
                "before": before,
                "after": None,
                "backup_identity_matches": backup_identity_matches,
            }

        rename_acknowledged = True
        try:
            connection.execute(
                text(
                    "RENAME TABLE "
                    f"{_quoted_identifier(LIVE_TABLE)} TO {_quoted_identifier(names.failed)}, "
                    f"{_quoted_identifier(names.backup)} TO {_quoted_identifier(LIVE_TABLE)}"
                )
            )
        except Exception:
            rename_acknowledged = False

        with self.engine.connect() as inspection_connection:
            after = self._table_facts(inspection_connection, names, marker)
            restored_live_identity_matches = (
                previous_live_identity is not None
                and after["live"]["exists"]
                and self._table_identity(inspection_connection, LIVE_TABLE)
                == previous_live_identity
            )
        restored = (
            after["live"]["exists"]
            and not after["live"]["owned"]
            and not after["backup"]["exists"]
            and after["failed"]["owned"]
            and not after["staging"]["exists"]
            and restored_live_identity_matches
        )
        return {
            "state": "restored" if restored else "unknown",
            "before": before,
            "after": after,
            "rename_acknowledged": rename_acknowledged,
            "failed_table_retained": restored,
            "backup_identity_matches": backup_identity_matches,
            "restored_live_identity_matches": restored_live_identity_matches,
        }

    def _quarantine_failed_first_publish(
        self,
        names: ImportTableNames,
        *,
        marker: str,
        connection: Any,
    ) -> dict[str, Any]:
        """Isolate a known-invalid first live table without deleting it."""

        with self.engine.connect() as inspection_connection:
            before = self._table_facts(inspection_connection, names, marker)
        authorized = (
            before["live"]["owned"]
            and not before["staging"]["exists"]
            and not before["backup"]["exists"]
            and not before["failed"]["exists"]
        )
        if not authorized:
            return {"state": "unknown", "before": before, "after": None}

        rename_acknowledged = True
        try:
            connection.execute(
                text(
                    f"RENAME TABLE {_quoted_identifier(LIVE_TABLE)} "
                    f"TO {_quoted_identifier(names.failed)}"
                )
            )
        except Exception:
            rename_acknowledged = False

        with self.engine.connect() as inspection_connection:
            after = self._table_facts(inspection_connection, names, marker)
        quarantined = (
            not after["live"]["exists"]
            and not after["staging"]["exists"]
            and not after["backup"]["exists"]
            and after["failed"]["owned"]
        )
        return {
            "state": "quarantined" if quarantined else "unknown",
            "before": before,
            "after": after,
            "rename_acknowledged": rename_acknowledged,
            "failed_table_retained": quarantined,
        }

    def _table_identity(
        self, connection: Any, table_name: str
    ) -> TableIdentity:
        """Capture row, comment, column, and index facts that survive rename."""

        return TableIdentity(
            row_count=_count_rows(connection, table_name),
            comment=_table_comment(
                connection, self.settings.mysql_database, table_name
            )
            or "",
            schema_fingerprint=self._table_schema_fingerprint(
                connection, table_name
            ),
        )

    def _table_schema_fingerprint(
        self, connection: Any, table_name: str
    ) -> str:
        """Hash stable MySQL table/column/index metadata independent of its name."""

        table_row = connection.execute(
            text(
                """
                SELECT ENGINE, TABLE_COLLATION, ROW_FORMAT
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                """
            ),
            {
                "schema_name": self.settings.mysql_database,
                "table_name": table_name,
            },
        ).one()
        column_rows = connection.execute(
            text(
                """
                SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                       EXTRA, GENERATION_EXPRESSION
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                ORDER BY ORDINAL_POSITION
                """
            ),
            {
                "schema_name": self.settings.mysql_database,
                "table_name": table_name,
            },
        ).all()
        index_rows = connection.execute(
            text(
                """
                SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME,
                       SUB_PART, INDEX_TYPE
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """
            ),
            {
                "schema_name": self.settings.mysql_database,
                "table_name": table_name,
            },
        ).all()
        normalized = {
            "table": _normalize_metadata_row(table_row),
            "columns": [_normalize_metadata_row(row) for row in column_rows],
            "indexes": [_normalize_metadata_row(row) for row in index_rows],
        }
        encoded = json.dumps(
            normalized,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _table_facts(
        self, connection: Any, names: ImportTableNames, marker: str
    ) -> dict[str, dict[str, bool]]:
        table_names = {
            "live": LIVE_TABLE,
            "staging": names.staging,
            "backup": names.backup,
            "failed": names.failed,
        }
        facts: dict[str, dict[str, bool]] = {}
        for label, table_name in table_names.items():
            exists = _table_exists(
                connection, self.settings.mysql_database, table_name
            )
            comment = (
                _table_comment(connection, self.settings.mysql_database, table_name)
                if exists
                else None
            )
            facts[label] = {"exists": exists, "owned": comment == marker}
        return facts

    def _cleanup_owned_staging(self, table_name: str, marker: str) -> str:
        with self.engine.begin() as connection:
            if not _table_exists(connection, self.settings.mysql_database, table_name):
                return "already_absent"
            if _table_comment(
                connection, self.settings.mysql_database, table_name
            ) != marker:
                raise ImportPipelineError(
                    "STAGING_CLEANUP_OWNERSHIP_MISMATCH",
                    "failed staging was retained because ownership could not be verified",
                )
            connection.execute(text(f"DROP TABLE {_quoted_identifier(table_name)}"))
        return "dropped_owned_staging"


def _scan_canonical_csv(path: Path) -> CsvVerification:
    """Hash and strictly validate header/width while file identity is stable."""

    try:
        before = path.stat()
        if before.st_size <= 0:
            raise SourceManifestError("cleaned CSV is empty")
        digest = hashlib.sha256()
        with path.open("rb") as handle:

            def decoded_lines() -> Any:
                for raw_line in handle:
                    digest.update(raw_line)
                    yield raw_line.decode("utf-8")

            reader = csv.reader(decoded_lines())
            try:
                header = next(reader)
            except StopIteration as exc:
                raise SourceManifestError("cleaned CSV has no header") from exc
            if header != list(CANONICAL_COLUMNS):
                raise SourceManifestError(
                    "cleaned CSV columns do not match the canonical schema"
                )
            row_count = 0
            for row in reader:
                row_count += 1
                if len(row) != len(CANONICAL_COLUMNS):
                    raise SourceManifestError(
                        "cleaned CSV contains a row with a non-canonical field count"
                    )
        after = path.stat()
    except SourceManifestError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise SourceManifestError("cleaned CSV could not be strictly validated") from exc

    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise SourceManifestError("cleaned CSV changed while it was being verified")
    return CsvVerification(
        sha256=digest.hexdigest(),
        row_count=row_count,
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        device=after.st_dev,
        inode=after.st_ino,
    )


def _manifest_facts(manifest: dict[str, Any], csv_path: Path) -> dict[str, Any]:
    if manifest.get("status") != "succeeded":
        raise SourceManifestError("cleaning manifest is not successful")
    if manifest.get("schema_version") != SPARCS_SCHEMA_VERSION:
        raise SourceManifestError("cleaning manifest schema version is unsupported")
    if manifest.get("error") is not None:
        raise SourceManifestError("successful cleaning manifest must not contain an error")

    source_payload = _mapping(manifest.get("source"), "source")
    output_payload = _mapping(manifest.get("output"), "output")
    quality_payload = _mapping(manifest.get("quality"), "quality")
    if output_payload.get("published") is not True:
        raise SourceManifestError("cleaned output was not atomically published")
    manifest_output_path = Path(str(output_payload.get("path", ""))).expanduser().resolve()
    if manifest_output_path != csv_path:
        raise SourceManifestError("manifest output path does not match the cleaned CSV")
    if quality_payload.get("schema_valid") is not True:
        raise SourceManifestError("cleaning manifest did not pass schema validation")
    if quality_payload.get("row_counts_reconciled") is not True:
        raise SourceManifestError("cleaning manifest did not reconcile row counts")
    if quality_payload.get("source_complete") is not True:
        raise SourceManifestError("partial-source publication is disabled")

    manifest_rows = _positive_int(output_payload.get("row_count"), "output.row_count")
    quality_rows = _positive_int(quality_payload.get("output_rows"), "quality.output_rows")
    input_rows = _nonnegative_int(quality_payload.get("input_rows"), "quality.input_rows")
    rejected_rows = _nonnegative_int(
        quality_payload.get("rejected_rows"), "quality.rejected_rows"
    )
    max_rejected = _nonnegative_int(
        quality_payload.get("max_rejected_rows"), "quality.max_rejected_rows"
    )
    if manifest_rows != quality_rows or input_rows != quality_rows + rejected_rows:
        raise SourceManifestError("cleaning manifest row counts are inconsistent")
    if rejected_rows > max_rejected:
        raise SourceManifestError("cleaning manifest exceeded its rejection threshold")

    cleaning_task_id = str(manifest.get("task_id") or "")
    try:
        UUID(cleaning_task_id)
    except ValueError as exc:
        raise SourceManifestError("cleaning manifest task_id must be a UUID") from exc
    raw_source_path = str(source_payload.get("path") or "").strip()
    if not raw_source_path:
        raise SourceManifestError("cleaning manifest source.path is required")
    _positive_int(source_payload.get("size_bytes"), "source.size_bytes")
    _nonnegative_int(source_payload.get("mtime_ns"), "source.mtime_ns")
    raw_hash = _sha256(source_payload.get("sha256"), "source.sha256")
    return {
        "row_count": manifest_rows,
        "sha256": _sha256(output_payload.get("sha256"), "output.sha256"),
        "cleaning_task_id": cleaning_task_id,
        "raw_source_sha256": raw_hash,
        "schema_version": SPARCS_SCHEMA_VERSION,
    }


def _read_manifest_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    """Parse and hash one stable manifest byte snapshot."""

    if not path.is_file():
        raise SourceManifestError("cleaning manifest does not exist")
    try:
        before = path.stat()
        if before.st_size > MAX_MANIFEST_BYTES:
            raise SourceManifestError("cleaning manifest exceeds the size limit")
        raw = path.read_bytes()
        after = path.stat()
    except SourceManifestError:
        raise
    except OSError as exc:
        raise SourceManifestError("cleaning manifest could not be read") from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(raw) != after.st_size:
        raise SourceManifestError("cleaning manifest changed while it was being read")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SourceManifestError("cleaning manifest exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceManifestError("cleaning manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SourceManifestError("cleaning manifest must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    """Read one stable manifest snapshot for compatibility callers."""

    return _read_manifest_snapshot(path)[0]


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceManifestError(f"cleaning manifest {field} must be an object")
    return value


def _normalize_metadata_row(row: Any) -> list[str | None]:
    """Convert a SQLAlchemy metadata row into deterministic JSON scalars."""

    return [None if value is None else str(value) for value in tuple(row)]


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed <= 0:
        raise SourceManifestError(f"cleaning manifest {field} must be positive")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceManifestError(
            f"cleaning manifest {field} must be a non-negative integer"
        )
    return value


def _sha256(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise SourceManifestError(f"{field} must be a SHA-256 digest")
    return normalized


def _valid_marker(marker: str) -> bool:
    return bool(
        re.fullmatch(
            rf"{re.escape(MARKER_PREFIX)};task_id=[0-9a-f]{{32}};source_sha256=[0-9a-f]{{64}}",
            marker,
        )
    )


def _quoted_identifier(identifier: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError("unsafe SQL identifier")
    return f"`{identifier}`"


def _table_exists(connection: Any, schema_name: str, table_name: str) -> bool:
    return bool(
        connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                """
            ),
            {"schema_name": schema_name, "table_name": table_name},
        ).scalar_one()
    )


def _table_comment(connection: Any, schema_name: str, table_name: str) -> str | None:
    return connection.execute(
        text(
            """
            SELECT TABLE_COMMENT
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = :schema_name
              AND TABLE_NAME = :table_name
            """
        ),
        {"schema_name": schema_name, "table_name": table_name},
    ).scalar_one_or_none()


def _count_rows(connection: Any, table_name: str) -> int:
    return int(
        connection.execute(
            text(f"SELECT COUNT(*) FROM {_quoted_identifier(table_name)}")
        ).scalar_one()
    )


def _staging_table(table_name: str, *, marker: str) -> Table:
    _quoted_identifier(table_name)
    if not _valid_marker(marker):
        raise ValueError("unsafe table ownership marker")
    metadata = MetaData()
    specification = get_table_spec(LIVE_TABLE)
    return Table(
        table_name,
        metadata,
        *(
            Column(column.name, sqlalchemy_type(column.data_type))
            for column in specification.columns.values()
        ),
        comment=marker,
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def _is_local_infile_unavailable(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        args = getattr(current, "args", ())
        if args and args[0] in {1148, 2068, 3948}:
            return True
        current = current.__cause__ or current.__context__
    return False


def new_import_task_id() -> str:
    """Return a new UUID string for CLI callers."""

    return str(uuid4())
