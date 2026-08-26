"""First-release email identity boundary tests."""

import pytest

from medical_ai.identity.emails import normalize_ascii_email


@pytest.mark.parametrize(
    "value",
    ["K@example.com", "ſ@example.com", "ß@example.com"],
)
def test_unicode_compatibility_characters_cannot_fold_into_ascii_identity(
    value: str,
) -> None:
    assert normalize_ascii_email(value) is None


def test_ascii_identity_is_trimmed_and_case_folded() -> None:
    assert normalize_ascii_email(" Analyst@Example.COM ") == "analyst@example.com"
