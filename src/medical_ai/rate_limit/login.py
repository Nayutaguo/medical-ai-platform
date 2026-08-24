"""Redis-backed, privacy-preserving login rate limiting.

The limiter evaluates the source-address bucket and normalized-account bucket in
one Lua script.  Redis therefore observes only keyed HMAC digests and either
increments both buckets or neither when the command cannot be executed.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
from typing import Any, Protocol, runtime_checkable


_LOGIN_FIXED_WINDOW_SCRIPT = """
local ip_count = redis.call('INCR', KEYS[1])
if ip_count == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[3])
end

local account_count = redis.call('INCR', KEYS[2])
if account_count == 1 then
    redis.call('PEXPIRE', KEYS[2], ARGV[3])
end

local ip_ttl = redis.call('PTTL', KEYS[1])
if ip_ttl < 0 then
    redis.call('PEXPIRE', KEYS[1], ARGV[3])
    ip_ttl = tonumber(ARGV[3])
end

local account_ttl = redis.call('PTTL', KEYS[2])
if account_ttl < 0 then
    redis.call('PEXPIRE', KEYS[2], ARGV[3])
    account_ttl = tonumber(ARGV[3])
end

local retry_after_ms = 0
if ip_count > tonumber(ARGV[1]) then
    retry_after_ms = ip_ttl
end
if account_count > tonumber(ARGV[2]) and account_ttl > retry_after_ms then
    retry_after_ms = account_ttl
end

if retry_after_ms > 0 then
    return {0, retry_after_ms}
end
return {1, 0}
"""


class LoginRateLimitExceeded(RuntimeError):
    """Raised when either login-protection bucket is exhausted."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Login rate limit exceeded")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class LoginRateLimitUnavailable(RuntimeError):
    """Raised when an enabled login-protection dependency cannot decide."""


@runtime_checkable
class LoginRateLimiter(Protocol):
    """Port used by the HTTP adapter before password verification begins."""

    def check(self, *, source_ip: str, normalized_account: str) -> None:
        """Consume both login buckets or raise a stable limiter exception."""


class RedisLoginRateLimiter:
    """Atomically enforce fixed-window limits for IP and account dimensions."""

    def __init__(
        self,
        redis_client: Any,
        *,
        key_secret: str,
        ip_attempt_limit: int,
        account_attempt_limit: int,
        window_seconds: int,
        key_prefix: str = "medical-ai:rate-limit:login",
    ) -> None:
        secret_bytes = key_secret.encode("utf-8")
        if len(secret_bytes) < 32:
            raise ValueError("Login rate-limit key secret must contain at least 32 UTF-8 bytes")
        if ip_attempt_limit < 1 or account_attempt_limit < 1 or window_seconds < 1:
            raise ValueError("Login rate-limit values must be positive")
        self._redis = redis_client
        self._key_secret = secret_bytes
        self._ip_attempt_limit = ip_attempt_limit
        self._account_attempt_limit = account_attempt_limit
        self._window_ms = window_seconds * 1000
        self._key_prefix = key_prefix

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        **kwargs: Any,
    ) -> "RedisLoginRateLimiter":
        """Build a lazy redis-py client without opening a network connection."""

        try:
            from redis import Redis

            client = Redis.from_url(
                redis_url,
                decode_responses=False,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
        except Exception as exc:
            raise LoginRateLimitUnavailable("Login protection is unavailable") from exc
        return cls(client, **kwargs)

    def check(self, *, source_ip: str, normalized_account: str) -> None:
        """Atomically increment both hashed buckets and reject exhausted ones."""

        ip_key = self._bucket_key("ip", _canonical_ip(source_ip))
        account_key = self._bucket_key(
            "account",
            normalized_account.strip().casefold() or "invalid-login-identifier",
        )
        try:
            result = self._redis.eval(
                _LOGIN_FIXED_WINDOW_SCRIPT,
                2,
                ip_key,
                account_key,
                self._ip_attempt_limit,
                self._account_attempt_limit,
                self._window_ms,
            )
            allowed = int(result[0])
            retry_after_ms = int(result[1])
        except Exception as exc:
            raise LoginRateLimitUnavailable("Login protection is unavailable") from exc

        if allowed != 1:
            retry_after_seconds = math.ceil(max(1, retry_after_ms) / 1000)
            raise LoginRateLimitExceeded(retry_after_seconds)

    def _bucket_key(self, dimension: str, value: str) -> str:
        digest = hmac.new(
            self._key_secret,
            f"{dimension}\x00{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{self._key_prefix}:{dimension}:{digest}"


def _canonical_ip(value: str) -> str:
    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate:
        return "unknown-source"
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        # The value still remains protected by the keyed digest.  Flask's
        # remote_addr is server-derived; forwarded headers are never consulted.
        return candidate.casefold()
