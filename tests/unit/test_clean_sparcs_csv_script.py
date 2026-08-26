import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from medical_ai.data import CANONICAL_COLUMNS, RAW_TO_CANONICAL

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "clean_sparcs_csv.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("clean_sparcs_csv_script", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
cleaner = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(cleaner)


def _write_source(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RAW_TO_CANONICAL), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            complete_row = {column: "" for column in RAW_TO_CANONICAL}
            complete_row.update(row)
            writer.writerow(complete_row)


def _valid_row() -> dict[str, str]:
    return {
        "Permanent Facility Id": "001169",
        "Age Group": "0 to 17",
        "Length of Stay": "2",
        "Type of Admission": "Emergency",
        "Discharge Year": "2021",
        "Birth Weight": "3000",
        "Total Charges": "1,234.50",
        "Total Costs": "456.25",
    }


def test_failed_cleaning_preserves_existing_output_and_writes_safe_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "clean.csv"
    _write_source(source, [{**_valid_row(), "Total Charges": "private-bad-value"}])
    output.write_text("previous-output\n", encoding="utf-8")

    with pytest.raises(cleaner.CleaningRunError) as caught:
        cleaner.clean_csv(
            input_path=source,
            output_path=output,
            task_id="task-failed",
            progress_every=0,
        )

    assert caught.value.error_code == "MAX_REJECTED_ROWS_EXCEEDED"
    assert output.read_text(encoding="utf-8") == "previous-output\n"
    assert not list(tmp_path.glob(".clean.csv.*.tmp"))

    manifest = json.loads((tmp_path / "clean.manifest.json").read_text(encoding="utf-8"))
    profile = json.loads((tmp_path / "clean.profile.json").read_text(encoding="utf-8"))
    assert manifest["task_id"] == "task-failed"
    assert manifest["status"] == "failed"
    assert manifest["output"] == {
        "path": str(output.resolve()),
        "row_count": None,
        "sha256": None,
        "published": False,
    }
    assert manifest["quality"]["input_rows"] == 1
    assert manifest["quality"]["rejected_rows"] == 1
    assert manifest["error"]["code"] == "MAX_REJECTED_ROWS_EXCEEDED"
    assert profile["invalid_row_count"] == 1
    assert profile["invalid_rows_sample"] == [{"row_number": 2, "error_code": "VALUE_ERROR"}]
    assert "private-bad-value" not in json.dumps(manifest)
    assert "private-bad-value" not in json.dumps(profile)


def test_success_atomically_replaces_output_and_emits_loader_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "clean.csv"
    _write_source(source, [_valid_row()])
    output.write_text("previous-output\n", encoding="utf-8")
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def tracking_replace(source_path: str | Path, destination_path: str | Path) -> None:
        replacements.append((Path(source_path), Path(destination_path)))
        real_replace(source_path, destination_path)

    monkeypatch.setattr(cleaner.os, "replace", tracking_replace)

    manifest = cleaner.clean_csv(
        input_path=source,
        output_path=output,
        task_id="task-success",
        progress_every=0,
    )

    csv_replacements = [item for item in replacements if item[1] == output]
    assert len(csv_replacements) == 1
    assert csv_replacements[0][0].parent == output.parent
    assert csv_replacements[0][0].name.startswith(f".{output.name}.")

    stored_manifest = json.loads((tmp_path / "clean.manifest.json").read_text(encoding="utf-8"))
    stored_profile = json.loads((tmp_path / "clean.profile.json").read_text(encoding="utf-8"))
    assert manifest == stored_manifest
    assert manifest["status"] == "succeeded"
    assert manifest["schema_version"] == cleaner.SCHEMA_VERSION
    assert manifest["source"]["size_bytes"] == source.stat().st_size
    assert manifest["source"]["mtime_ns"] == source.stat().st_mtime_ns
    assert manifest["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["output"]["path"] == str(output.resolve())
    assert manifest["output"]["row_count"] == 1
    assert manifest["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["output"]["published"] is True
    assert manifest["quality"] == {
        "input_rows": 1,
        "output_rows": 1,
        "rejected_rows": 0,
        "max_rejected_rows": 0,
        "schema_valid": True,
        "row_counts_reconciled": True,
        "source_complete": True,
        "min_output_rows": 1,
    }
    assert manifest["error"] is None
    assert manifest["started_at"]
    assert manifest["finished_at"]
    assert stored_profile["task_id"] == manifest["task_id"]
    assert stored_profile["source"] == manifest["source"]
    assert stored_profile["output"] == manifest["output"]
    assert stored_profile["quality"] == manifest["quality"]
    assert stored_profile["started_at"] == manifest["started_at"]
    assert stored_profile["finished_at"] == manifest["finished_at"]
    assert stored_profile["error_code"] is None

    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["BirthWeight"] == ""
    assert list(rows[0]) == CANONICAL_COLUMNS


def test_explicit_rejection_budget_allows_reconciled_publish(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "clean.csv"
    rejected = {**_valid_row(), "Total Costs": "not-a-decimal"}
    _write_source(source, [_valid_row(), rejected])

    manifest = cleaner.clean_csv(
        input_path=source,
        output_path=output,
        task_id="task-budget",
        progress_every=0,
        max_rejected_rows=1,
    )

    assert manifest["status"] == "succeeded"
    assert manifest["quality"]["input_rows"] == 2
    assert manifest["quality"]["output_rows"] == 1
    assert manifest["quality"]["rejected_rows"] == 1
    assert manifest["quality"]["row_counts_reconciled"] is True
    assert manifest["output"]["row_count"] == 1


def test_default_source_path_uses_the_actual_medical_data_directory() -> None:
    assert "医养项目数据" in cleaner.DEFAULT_RAW_CSV.parts
    assert "009 医养项目数据" not in cleaner.DEFAULT_RAW_CSV.parts


@pytest.mark.parametrize("collision", ["source", "output"])
def test_conflicting_artifact_paths_never_overwrite_source_or_output(tmp_path: Path, collision: str) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "clean.csv"
    _write_source(source, [_valid_row()])
    source_before = source.read_bytes()
    output.write_text("previous-output\n", encoding="utf-8")
    kwargs = {"profile_output": source} if collision == "source" else {"manifest_output": output}

    with pytest.raises(cleaner.CleaningRunError) as caught:
        cleaner.clean_csv(
            input_path=source,
            output_path=output,
            task_id="task-conflict",
            progress_every=0,
            **kwargs,
        )

    assert caught.value.error_code == "INVALID_CONFIGURATION"
    assert source.read_bytes() == source_before
    assert output.read_text(encoding="utf-8") == "previous-output\n"


def test_manifest_publish_failure_rolls_back_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "clean.csv"
    manifest_path = tmp_path / "clean.manifest.json"
    _write_source(source, [_valid_row()])
    output.write_text("previous-output\n", encoding="utf-8")
    real_replace = os.replace
    failed_once = False

    def fail_first_manifest_replace(source_path: str | Path, destination_path: str | Path) -> None:
        nonlocal failed_once
        if Path(destination_path) == manifest_path and not failed_once:
            failed_once = True
            raise OSError("simulated manifest publish failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(cleaner.os, "replace", fail_first_manifest_replace)

    with pytest.raises(cleaner.CleaningRunError) as caught:
        cleaner.clean_csv(
            input_path=source,
            output_path=output,
            task_id="task-publish-failure",
            progress_every=0,
        )

    assert caught.value.error_code == "ARTIFACT_PUBLISH_FAILED"
    assert output.read_text(encoding="utf-8") == "previous-output\n"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["output"]["published"] is False


def test_source_hash_change_during_cleaning_rejects_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "clean.csv"
    _write_source(source, [_valid_row()])
    output.write_text("previous-output\n", encoding="utf-8")
    real_sha256_file = cleaner._sha256_file
    source_hash_calls = 0

    def changing_source_hash(path: Path) -> str:
        nonlocal source_hash_calls
        if Path(path) == source:
            source_hash_calls += 1
            if source_hash_calls == 2:
                return "f" * 64
        return real_sha256_file(path)

    monkeypatch.setattr(cleaner, "_sha256_file", changing_source_hash)

    with pytest.raises(cleaner.CleaningRunError) as caught:
        cleaner.clean_csv(
            input_path=source,
            output_path=output,
            task_id="task-source-changed",
            progress_every=0,
        )

    assert caught.value.error_code == "SOURCE_CHANGED"
    assert output.read_text(encoding="utf-8") == "previous-output\n"
    manifest = json.loads((tmp_path / "clean.manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"]["code"] == "SOURCE_CHANGED"
