from __future__ import annotations

import http.client
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from medical_ai.agent.llm_config import LLMConfig, get_llm_config

LOGGER = logging.getLogger(__name__)
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
CLIENT_USER_AGENT = "medical-ai-platform/0.1"


class LLMClientError(RuntimeError):
    """Base class for safe, transport-level LLM client failures."""


class LLMTimeoutError(LLMClientError):
    """Raised when the upstream model does not respond before the deadline."""


class LLMResponseError(LLMClientError):
    """Raised when the upstream model returns an unusable HTTP response."""


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class OpenAICompatibleClient:
    def __init__(
        self,
        config: LLMConfig | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        retry_base_delay_seconds: float | None = None,
    ) -> None:
        self.config = config or get_llm_config()
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds
        self.max_retries = max_retries if max_retries is not None else self.config.max_retries
        self.retry_base_delay_seconds = (
            retry_base_delay_seconds
            if retry_base_delay_seconds is not None
            else self.config.retry_base_delay_seconds
        )

    def chat_json(self, messages: list[ChatMessage], max_tokens: int = 1200) -> str:
        if not self.config.configured:
            raise RuntimeError("LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL in .env.")

        payload = {
            "model": self.config.model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            url=self._chat_completions_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": CLIENT_USER_AGENT,
            },
            method="POST",
        )

        last_error: LLMClientError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw_body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    status = int(getattr(response, "status", 200))
                body = self._parse_response(raw_body, content_type)
                content = self._extract_content(body)
                LOGGER.info(
                    "llm_request_completed attempt=%s status=%s duration_ms=%.3f response_bytes=%s",
                    attempt + 1,
                    status,
                    (time.perf_counter() - started) * 1000,
                    len(raw_body),
                )
                return content
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_HTTP_STATUSES:
                    raise LLMClientError(f"LLM API rejected the request with HTTP {exc.code}") from exc
                last_error = (
                    LLMTimeoutError("LLM API gateway timed out")
                    if exc.code == 504
                    else LLMClientError(f"LLM API temporarily unavailable with HTTP {exc.code}")
                )
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    last_error = LLMTimeoutError("LLM API request timed out")
                else:
                    last_error = LLMClientError("LLM API network request failed")
            except (TimeoutError, socket.timeout):
                last_error = LLMTimeoutError("LLM API request timed out")
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                last_error = LLMClientError("LLM API closed the connection unexpectedly")
            except LLMResponseError as exc:
                last_error = exc

            if attempt >= self.max_retries:
                assert last_error is not None
                raise last_error

            delay_seconds = self.retry_base_delay_seconds * (2**attempt)
            LOGGER.warning(
                "llm_request_retry attempt=%s max_attempts=%s error_type=%s delay_seconds=%.3f",
                attempt + 1,
                self.max_retries + 1,
                type(last_error).__name__,
                delay_seconds,
            )
            time.sleep(delay_seconds)

        raise LLMClientError("LLM API request failed")

    def _chat_completions_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _parse_response(self, raw_body: bytes, content_type: str) -> dict[str, Any]:
        if not raw_body.strip():
            raise LLMResponseError("LLM API returned an empty response")
        if content_type and "json" not in content_type.lower():
            raise LLMResponseError("LLM API returned a non-JSON response")
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMResponseError("LLM API returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise LLMResponseError("LLM API returned an unexpected JSON value")
        return body

    def _extract_content(self, body: dict[str, Any]) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("LLM API returned an unexpected response shape") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("LLM API returned empty message content")
        return content
