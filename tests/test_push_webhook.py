"""Unit tests for webhook signing and retry policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from AutoGLM_GUI.push.webhook import (
    BACKOFF_SECONDS,
    next_retry_at,
    sign_body,
    validate_callback_url,
)


def test_sign_body_is_deterministic_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    signature = sign_body("secret", b'{"a":1}')
    assert signature == sign_body("secret", b'{"a":1}')
    assert signature != sign_body("other", b'{"a":1}')
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64


def test_validate_callback_url_rejects_non_http() -> None:
    assert validate_callback_url(None) == (True, "")
    assert validate_callback_url("https://example.com/hook")[0] is True
    assert validate_callback_url("http://example.com/hook")[0] is True
    assert validate_callback_url("ftp://example.com/hook")[0] is False


def test_callback_host_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOGLM_WEBHOOK_ALLOWED_HOSTS", "example.com,corp.internal")
    assert validate_callback_url("https://example.com/hook")[0] is True
    assert validate_callback_url("https://api.example.com/hook")[0] is True
    assert validate_callback_url("https://corp.internal/hook")[0] is True
    assert validate_callback_url("https://evil.test/hook")[0] is False


def test_next_retry_at_uses_backoff_then_exhausts() -> None:
    now = datetime.now(timezone.utc)
    assert next_retry_at(0, now=now) == now + timedelta(seconds=BACKOFF_SECONDS[0])
    assert next_retry_at(len(BACKOFF_SECONDS), now=now) is None
