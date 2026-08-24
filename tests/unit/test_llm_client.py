import http.client
import json
import socket
import urllib.error
from unittest.mock import patch

import pytest

from medical_ai.agent.llm_client import (
    ChatMessage,
    LLMClientError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleClient,
)
from medical_ai.agent.llm_config import LLMConfig


class FakeHTTPResponse:
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200) -> None:
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _config() -> LLMConfig:
    return LLMConfig(base_url="https://llm.example/v1", api_key="test-key", model="test-model")


def _valid_response(content: str = '{"ok": true}') -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def test_empty_response_is_retried_then_succeeds() -> None:
    client = OpenAICompatibleClient(
        config=_config(),
        max_retries=1,
        retry_base_delay_seconds=0,
    )

    with (
        patch(
            "medical_ai.agent.llm_client.urllib.request.urlopen",
            side_effect=[FakeHTTPResponse(b""), FakeHTTPResponse(_valid_response())],
        ) as urlopen,
        patch("medical_ai.agent.llm_client.time.sleep") as sleep,
    ):
        content = client.chat_json([ChatMessage(role="user", content="test")])

    assert content == '{"ok": true}'
    assert urlopen.call_count == 2
    sleep.assert_called_once_with(0)


def test_invalid_json_response_raises_safe_error_after_retry_exhaustion() -> None:
    client = OpenAICompatibleClient(
        config=_config(),
        max_retries=1,
        retry_base_delay_seconds=0,
    )

    with patch(
        "medical_ai.agent.llm_client.urllib.request.urlopen",
        side_effect=[FakeHTTPResponse(b"not-json"), FakeHTTPResponse(b"not-json")],
    ):
        with pytest.raises(LLMResponseError, match="invalid JSON"):
            client.chat_json([ChatMessage(role="user", content="test")])


def test_non_json_content_type_is_rejected() -> None:
    client = OpenAICompatibleClient(config=_config(), max_retries=0)

    with patch(
        "medical_ai.agent.llm_client.urllib.request.urlopen",
        return_value=FakeHTTPResponse(b"<html>bad gateway</html>", "text/html"),
    ):
        with pytest.raises(LLMResponseError, match="non-JSON"):
            client.chat_json([ChatMessage(role="user", content="test")])


def test_network_timeout_raises_timeout_error() -> None:
    client = OpenAICompatibleClient(config=_config(), max_retries=0)

    with patch(
        "medical_ai.agent.llm_client.urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.timeout("timed out")),
    ):
        with pytest.raises(LLMTimeoutError, match="timed out"):
            client.chat_json([ChatMessage(role="user", content="test")])


def test_non_transient_http_error_is_not_retried() -> None:
    client = OpenAICompatibleClient(config=_config(), max_retries=2)
    error = urllib.error.HTTPError(
        url="https://llm.example/v1/chat/completions",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=None,
    )

    with patch(
        "medical_ai.agent.llm_client.urllib.request.urlopen",
        side_effect=error,
    ) as urlopen:
        with pytest.raises(LLMClientError, match="HTTP 403"):
            client.chat_json([ChatMessage(role="user", content="test")])

    assert urlopen.call_count == 1


def test_remote_disconnect_is_retried_with_standard_client_headers() -> None:
    client = OpenAICompatibleClient(
        config=_config(),
        max_retries=1,
        retry_base_delay_seconds=0,
    )

    with patch(
        "medical_ai.agent.llm_client.urllib.request.urlopen",
        side_effect=[http.client.RemoteDisconnected(), FakeHTTPResponse(_valid_response())],
    ) as urlopen:
        content = client.chat_json([ChatMessage(role="user", content="test")])

    request = urlopen.call_args_list[0].args[0]
    assert content == '{"ok": true}'
    assert urlopen.call_count == 2
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent") == "medical-ai-platform/0.1"
