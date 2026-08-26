"""Canonical first-release email identifier policy.

MySQL's current ``utf8mb4_0900_ai_ci`` unique index is accent-insensitive.
Restricting account identifiers to ASCII prevents visually and semantically
distinct EAI addresses from collapsing onto one global identity until a future
binary-normalized schema migration is designed and reviewed.
"""

from __future__ import annotations


def normalize_ascii_email(value: object) -> str | None:
    """Return one normalized ASCII email or ``None`` when outside policy."""

    stripped = value.strip() if isinstance(value, str) else ""
    try:
        stripped.encode("ascii")
    except UnicodeEncodeError:
        return None
    normalized = stripped.casefold()
    encoded = normalized.encode("ascii")
    local, separator, domain = normalized.partition("@")
    if (
        not normalized
        or len(encoded) > 320
        or not separator
        or not local
        or not domain
        or "@" in domain
        or "." not in domain
    ):
        return None
    return normalized
