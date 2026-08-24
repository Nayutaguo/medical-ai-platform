"""Opaque authentication-token generation and one-way storage helpers."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from hmac import compare_digest


TOKEN_ENTROPY_BYTES = 32


@dataclass(frozen=True, repr=False)
class OpaqueToken:
    """A short-lived in-memory token value and its persistence-safe digest.

    ``repr=False`` prevents accidental disclosure in ordinary diagnostic
    output.  The raw value is returned only once to the authenticated client.
    """

    value: str
    digest: bytes


def generate_opaque_token() -> OpaqueToken:
    """Generate at least 256 bits of entropy for sessions or one-time use."""

    value = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    return OpaqueToken(value=value, digest=hash_opaque_token(value))


def hash_opaque_token(value: str) -> bytes:
    """Return the fixed-width SHA-256 digest stored in MySQL."""

    return hashlib.sha256(value.encode("utf-8")).digest()


def verify_opaque_token(value: str, expected_digest: bytes) -> bool:
    """Compare a presented token with a stored digest in constant time."""

    return compare_digest(hash_opaque_token(value), expected_digest)
