"""Outbound webhook delivery with HMAC signing and retry bookkeeping."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

BACKOFF_SECONDS = (1, 5, 30, 120, 600)

WEBHOOK_TIMEOUT_SECONDS = 10.0


def get_webhook_secret() -> str | None:
    secret = os.environ.get("AUTOGLM_WEBHOOK_SECRET")
    return secret or None


def sign_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _allowed_hosts() -> list[str]:
    raw = os.environ.get("AUTOGLM_WEBHOOK_ALLOWED_HOSTS", "")
    return [host.strip().lower() for host in raw.split(",") if host.strip()]


def validate_callback_url(url: str | None) -> tuple[bool, str]:
    """Reject non-HTTP(S) URLs and hosts outside the optional allowlist."""
    if not url:
        return True, ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "callback_url must use http or https"
    if not parsed.hostname:
        return False, "callback_url must include a host"
    allowed = _allowed_hosts()
    if allowed:
        host = parsed.hostname.lower()
        if not any(
            host == item or host.endswith(f".{item.lstrip('.')}") for item in allowed
        ):
            return (
                False,
                f"callback host {host} is not in AUTOGLM_WEBHOOK_ALLOWED_HOSTS",
            )
    return True, ""


def build_headers(event: str, body: bytes) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-AutoGLM-Event": event,
    }
    secret = get_webhook_secret()
    if secret:
        headers["X-AutoGLM-Signature"] = sign_body(secret, body)
    return headers


async def deliver_webhook(
    delivery: dict[str, Any],
) -> tuple[bool, str | None]:
    payload = str(delivery["payload_json"])
    body = payload.encode("utf-8")
    headers = build_headers(str(delivery["event"]), body)
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(
                str(delivery["url"]), content=body, headers=headers
            )
    except Exception as exc:  # network errors are retried
        return False, f"{type(exc).__name__}: {exc}"

    if 200 <= response.status_code < 300:
        return True, None
    return False, f"HTTP {response.status_code}: {response.text[:200]}"


def next_retry_at(attempts: int, *, now: datetime | None = None) -> datetime | None:
    """Return the next retry time, or None when attempts are exhausted."""
    if attempts >= len(BACKOFF_SECONDS):
        return None
    base = now or datetime.now(timezone.utc)
    return base + timedelta(seconds=BACKOFF_SECONDS[attempts])


def dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
