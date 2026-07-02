from __future__ import annotations

import base64
import hmac
import hashlib
from datetime import datetime, timezone


def format_okx_timestamp(dt: datetime | None = None) -> str:
    """UTC ISO-8601 timestamp with millisecond precision, e.g. 2020-12-08T09:08:57.715Z."""
    current = dt or datetime.now(timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%S.") + f"{current.microsecond // 1000:03d}Z"


def build_okx_prehash(
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> str:
    """Build OKX REST sign prehash: timestamp + METHOD + requestPath + body."""
    return f"{timestamp}{method.upper()}{request_path}{body}"


def sign_okx_request(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> str:
    """Return Base64(HMAC_SHA256(secret, prehash))."""
    prehash = build_okx_prehash(timestamp, method, request_path, body)
    digest = hmac.new(
        secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")
