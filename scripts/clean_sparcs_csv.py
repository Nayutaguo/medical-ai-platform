#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from medical_ai.data import (
    CANONICAL_COLUMNS,
    RAW_TO_CANONICAL,
    DataQualityProfile,
    clean_sparcs_row,
    format_cleaned_row_for_csv,
)
from medical_ai.data.cleaning import SPARCS_SCHEMA_VERSION

DEFAULT_RAW_CSV = (
    ROOT.parent
    / "医养项目数据"
    / "Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv"
    / "Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv"
)
DEFAULT_OUTPUT = ROOT / "data" / "processed" / "inpatient_sparcs_2021_clean.csv"
SCHEMA_VERSION = SPARCS_SCHEMA_VERSION
HASH_CHUNK_SIZE = 1024 * 1024

ERROR_MESSAGES = {
    "INVALID_CONFIGURATION": "The cleaning configuration is invalid.",
    "SOURCE_NOT_FOUND": "The source CSV is unavailable.",
    "SOURCE_READ_FAILED": "The source CSV could not be read.",
    "SOURCE_CHANGED": "The source CSV changed while it was being cleaned.",
    "SCHEMA_MISMATCH": "The source CSV schema does not match the governed schema.",
    "ROW_REJECTED": "A source row could not be cleaned.",
    "MAX_REJECTED_ROWS_EXCEEDED": "Rejected rows exceed the configured maximum.",
    "ROW_COUNT_MISMATCH": "Input, output, and rejected row counts do not reconcile.",
    "NO_OUTPUT_ROWS": "The cleaning task produced no accepted rows.",
    "ARTIFACT_PUBLISH_FAILED": "The cleaning artifacts could not be published.",
    "INTERNAL_ERROR": "The cleaning task failed unexpectedly.",
}


class CleaningRunError(RuntimeError):
    """A cleaning failure with a stable, non-sensitive error code."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def clean_csv(
    *,
    input_path: Path,
    output_path: Path,
    profile_output: Path | None = None,
    manifest_output: Path | None = None,
    task_id: str | None = None,
    schema_version: str = SCHEMA_VERSION,
    limit: int | None = None,
    progress_every: int = 100000,
    on_error: str = "skip",
    max_rejected_rows: int = 0,
) -> dict[str, Any]:
    """Clean one CSV into an atomically published output plus profile and manifest."""

    input_path = Path(input_path)
    output_path = Path(output_path)
    profile_path = Path(profile_output) if profile_output else output_path.with_suffix(".profile.json")
    manifest_path = Path(manifest_output) if manifest_output else output_path.with_suffix(".manifest.json")
    task_identifier = task_id or uuid.uuid4().hex
    started_at = _utc_now()

    _validate_configuration(
        input_path=input_path,
        output_path=output_path,
        profile_path=profile_path,
        manifest_path=manifest_path,
        task_id=task_identifier,
        schema_version=schema_version,
        limit=limit,
        progress_every=progress_every,
        on_error=on_error,
        max_rejected_rows=max_rejected_rows,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    profile = DataQualityProfile()
    input_rows = 0
    schema_valid = False
    source_before: os.stat_result | None = None
    source = {
        "path": str(input_path.resolve()),
        "size_bytes": None,
        "mtime_ns": None,
        "sha256": None,
    }
    output_sha256: str | None = None
    published = False
    candidate_path: Path | None = None
    failure: Exception | None = None

    try:
        try:
            source_before = input_path.stat()
            source.update(
                {
                    "size_bytes": source_before.st_size,
                    "mtime_ns": source_before.st_mtime_ns,
                    "sha256": _sha256_file(input_path),
                }
            )
        except FileNotFoundError as exc:
            raise CleaningRunError("SOURCE_NOT_FOUND") from exc
        except OSError as exc:
            raise CleaningRunError("SOURCE_READ_FAILED") from exc

        candidate_path = _temporary_path_for(output_path)
        try:
            with input_path.open("r", encoding="utf-8-sig", newline="") as source_handle:
                reader = csv.DictReader(source_handle)
                _validate_schema(reader.fieldnames)
                schema_valid = True

                with candidate_path.open("w", encoding="utf-8", newline="") as target_handle:
                    writer = csv.DictWriter(
                        target_handle,
                        fieldnames=CANONICAL_COLUMNS,
                        lineterminator="\n",
                    )
                    writer.writeheader()

                    for row_number, row in enumerate(reader, start=2):
                        if limit is not None and input_rows >= limit:
                            break
                        input_rows += 1
                        try:
                            _validate_row_shape(row)
                            cleaned = clean_sparcs_row(row)
                        except ValueError as exc:
                            profile.record_invalid_row(row_number, exc)
                            if on_error == "fail":
                                raise CleaningRunError("ROW_REJECTED") from None
                            if profile.invalid_row_count > max_rejected_rows:
                                raise CleaningRunError("MAX_REJECTED_ROWS_EXCEEDED") from None
                            continue

                        profile.observe(cleaned)
                        writer.writerow(format_cleaned_row_for_csv(cleaned))
                        if progress_every and profile.row_count % progress_every == 0:
                            print(f"cleaned {profile.row_count} rows", file=sys.stderr)

                    target_handle.flush()
                    os.fsync(target_handle.fileno())
        except CleaningRunError:
            raise
        except OSError as exc:
            raise CleaningRunError("SOURCE_READ_FAILED") from exc

        if input_rows != profile.row_count + profile.invalid_row_count:
            raise CleaningRunError("ROW_COUNT_MISMATCH")
        if profile.invalid_row_count > max_rejected_rows:
            raise CleaningRunError("MAX_REJECTED_ROWS_EXCEEDED")
        if profile.row_count == 0:
            raise CleaningRunError("NO_OUTPUT_ROWS")
        if (
            source_before is None
            or not _source_is_unchanged(input_path, source_before)
            or _sha256_file(input_path) != source["sha256"]
        ):
            raise CleaningRunError("SOURCE_CHANGED")

        output_sha256 = _sha256_file(candidate_path)
    except Exception as exc:
        failure = exc

    published = failure is None
    status = "succeeded" if failure is None else "failed"
    error_code = None if failure is None else _error_code(failure)
    finished_at = _utc_now()
    row_counts_reconciled = input_rows == profile.row_count + profile.invalid_row_count
    manifest = {
        "task_id": task_identifier,
        "status": status,
        "schema_version": schema_version,
        "source": source,
        "output": {
            "path": str(output_path.resolve()),
            "row_count": profile.row_count if published else None,
            "sha256": output_sha256 if published else None,
            "published": published,
        },
        "quality": {
            "input_rows": input_rows,
            "output_rows": profile.row_count,
            "rejected_rows": profile.invalid_row_count,
            "max_rejected_rows": max_rejected_rows,
            "schema_valid": schema_valid,
            "row_counts_reconciled": row_counts_reconciled,
            "source_complete": failure is None and limit is None,
            "min_output_rows": 1,
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "error": None
        if error_code is None
        else {"code": error_code, "message": ERROR_MESSAGES.get(error_code, ERROR_MESSAGES["INTERNAL_ERROR"])},
    }
    profile_payload = {
        "task_id": task_identifier,
        "status": status,
        "schema_version": schema_version,
        "source": source,
        "output": manifest["output"],
        "quality": manifest["quality"],
        "started_at": started_at,
        "finished_at": finished_at,
        "error": manifest["error"],
        "error_code": error_code,
        "input_rows": input_rows,
        "output_rows": profile.row_count,
        "rejected_rows": profile.invalid_row_count,
        "max_rejected_rows": max_rejected_rows,
        **profile.to_dict(),
    }

    if failure is not None:
        if candidate_path is not None:
            candidate_path.unlink(missing_ok=True)
        _write_json_atomically(profile_path, profile_payload)
        _write_json_atomically(manifest_path, manifest)
        raise failure

    if candidate_path is None:
        raise CleaningRunError("ARTIFACT_PUBLISH_FAILED")

    try:
        _publish_success_artifacts(
            candidate_path=candidate_path,
            output_path=output_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            profile_payload=profile_payload,
            manifest=manifest,
        )
    except OSError as exc:
        publish_failure = CleaningRunError("ARTIFACT_PUBLISH_FAILED")
        publish_failure.__cause__ = exc
        _mark_artifacts_failed(manifest, profile_payload, publish_failure)
        _write_json_atomically(profile_path, profile_payload)
        _write_json_atomically(manifest_path, manifest)
        raise publish_failure

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean SPARCS inpatient discharge CSV into canonical project columns.")
    parser.add_argument("--input", type=Path, default=DEFAULT_RAW_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--profile-output", type=Path, default=None)
    parser.add_argument("--manifest-output", type=Path, default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--limit", type=int, default=None, help="Optional input-row limit for development runs.")
    parser.add_argument("--progress-every", type=int, default=100000)
    parser.add_argument("--on-error", choices=["fail", "skip"], default="skip")
    parser.add_argument(
        "--max-rejected-rows",
        type=int,
        default=0,
        help="Maximum rejected rows allowed by the quality gate; defaults to zero.",
    )
    args = parser.parse_args()

    try:
        manifest = clean_csv(
            input_path=args.input,
            output_path=args.output,
            profile_output=args.profile_output,
            manifest_output=args.manifest_output,
            task_id=args.task_id,
            limit=args.limit,
            progress_every=args.progress_every,
            on_error=args.on_error,
            max_rejected_rows=args.max_rejected_rows,
        )
    except Exception as exc:
        code = _error_code(exc)
        message = ERROR_MESSAGES.get(code, ERROR_MESSAGES["INTERNAL_ERROR"])
        print(f"Cleaning failed [{code}]: {message}", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Cleaned {manifest['output']['row_count']} rows into {manifest['output']['path']}")
    print(f"Wrote manifest for task {manifest['task_id']}")


def _validate_configuration(
    *,
    input_path: Path,
    output_path: Path,
    profile_path: Path,
    manifest_path: Path,
    task_id: str,
    schema_version: str,
    limit: int | None,
    progress_every: int,
    on_error: str,
    max_rejected_rows: int,
) -> None:
    paths = [input_path.resolve(), output_path.resolve(), profile_path.resolve(), manifest_path.resolve()]
    if len(set(paths)) != len(paths):
        raise CleaningRunError("INVALID_CONFIGURATION")
    if not task_id.strip() or not schema_version.strip():
        raise CleaningRunError("INVALID_CONFIGURATION")
    if limit is not None and limit <= 0:
        raise CleaningRunError("INVALID_CONFIGURATION")
    if progress_every < 0 or max_rejected_rows < 0:
        raise CleaningRunError("INVALID_CONFIGURATION")
    if on_error not in {"fail", "skip"}:
        raise CleaningRunError("INVALID_CONFIGURATION")


def _validate_schema(fieldnames: list[str] | None) -> None:
    actual = fieldnames or []
    expected = list(RAW_TO_CANONICAL)
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise CleaningRunError("SCHEMA_MISMATCH")


def _validate_row_shape(row: dict[Any, Any]) -> None:
    """Reject rows with extra or missing CSV cells before field cleaning."""

    if None in row or set(row) != set(RAW_TO_CANONICAL):
        raise ValueError("CSV row width does not match the governed schema")
    if any(row[column] is None for column in RAW_TO_CANONICAL):
        raise ValueError("CSV row width does not match the governed schema")


def _source_is_unchanged(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.stat()
    except OSError:
        return False
    return current.st_size == expected.st_size and current.st_mtime_ns == expected.st_mtime_ns


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_path_for(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def _stage_json(path: Path, payload: dict[str, Any]) -> Path:
    temporary_path = _temporary_path_for(path)
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = _stage_json(path, payload)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _publish_success_artifacts(
    *,
    candidate_path: Path,
    output_path: Path,
    profile_path: Path,
    manifest_path: Path,
    profile_payload: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Publish profile, CSV, then manifest; restore the prior CSV on failure."""

    staged_profile: Path | None = None
    staged_manifest: Path | None = None
    output_backup: Path | None = None
    output_replaced = False
    preserve_backup = False
    try:
        staged_profile = _stage_json(profile_path, profile_payload)
        staged_manifest = _stage_json(manifest_path, manifest)
        if output_path.exists():
            output_backup = _temporary_path_for(output_path)
            output_backup.unlink()
            os.link(output_path, output_backup)

        os.replace(staged_profile, profile_path)
        staged_profile = None
        os.replace(candidate_path, output_path)
        output_replaced = True
        os.replace(staged_manifest, manifest_path)
        staged_manifest = None
    except OSError as publish_error:
        if output_replaced:
            try:
                if output_backup is None:
                    output_path.unlink(missing_ok=True)
                else:
                    os.replace(output_backup, output_path)
                    output_backup = None
            except OSError as rollback_error:
                preserve_backup = True
                raise rollback_error from publish_error
        raise
    finally:
        candidate_path.unlink(missing_ok=True)
        if staged_profile is not None:
            staged_profile.unlink(missing_ok=True)
        if staged_manifest is not None:
            staged_manifest.unlink(missing_ok=True)
        if output_backup is not None and not preserve_backup:
            output_backup.unlink(missing_ok=True)


def _mark_artifacts_failed(
    manifest: dict[str, Any],
    profile_payload: dict[str, Any],
    failure: Exception,
) -> None:
    code = _error_code(failure)
    error = {"code": code, "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["INTERNAL_ERROR"])}
    finished_at = _utc_now()
    manifest["status"] = "failed"
    manifest["finished_at"] = finished_at
    manifest["output"] = {
        "path": manifest["output"]["path"],
        "row_count": None,
        "sha256": None,
        "published": False,
    }
    manifest["error"] = error
    profile_payload["status"] = "failed"
    profile_payload["output"] = manifest["output"]
    profile_payload["finished_at"] = finished_at
    profile_payload["error"] = error
    profile_payload["error_code"] = code


def _error_code(error: Exception) -> str:
    if isinstance(error, CleaningRunError):
        return error.error_code
    if isinstance(error, FileNotFoundError):
        return "SOURCE_NOT_FOUND"
    return "INTERNAL_ERROR"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
