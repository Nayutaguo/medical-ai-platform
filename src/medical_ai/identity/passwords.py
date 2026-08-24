"""Password policy and Argon2id hashing primitives.

This module deliberately has no Flask or database dependencies.  Callers may
persist only the encoded Argon2id verifier returned by :meth:`hash_password`;
plaintext passwords must remain request-local and must never be logged.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


class PasswordPolicyError(ValueError):
    """Raised when a new password does not satisfy the product policy."""


@dataclass(frozen=True)
class PasswordPolicy:
    """Bound password input while allowing passphrases and password managers."""

    minimum_characters: int = 12
    maximum_bytes: int = 1024

    def validate(self, password: str) -> None:
        """Validate a new password without applying composition rules."""

        if len(password) < self.minimum_characters:
            raise PasswordPolicyError(f"密码至少需要 {self.minimum_characters} 个字符")
        if len(password.encode("utf-8")) > self.maximum_bytes:
            raise PasswordPolicyError("密码超过允许的最大长度")


class Argon2idPasswordService:
    """Hash and verify passwords using explicit Argon2id parameters.

    The defaults are intentionally centralized so they can be tuned from a
    production benchmark without changing user-management services.
    """

    algorithm = "argon2id"

    def __init__(
        self,
        *,
        policy: PasswordPolicy | None = None,
        time_cost: int = 3,
        memory_cost_kib: int = 65_536,
        parallelism: int = 2,
    ) -> None:
        self.policy = policy or PasswordPolicy()
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash_password(self, password: str) -> str:
        """Validate and return an encoded Argon2id password verifier."""

        self.policy.validate(password)
        return self._hasher.hash(password)

    def verify_password(self, encoded_hash: str, password: str) -> bool:
        """Return ``False`` for mismatches and malformed/untrusted hashes."""

        if not encoded_hash.startswith("$argon2id$"):
            return False
        try:
            return self._hasher.verify(encoded_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        """Report whether a valid verifier uses outdated cost parameters."""

        if not encoded_hash.startswith("$argon2id$"):
            return True
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except InvalidHashError:
            return True
