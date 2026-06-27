from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

REDACTED_VALUE = "***REDACTED***"

SENSITIVE_KEYS = frozenset(
    {
        "secret",
        "webhook_secret",
        "binance_api_key",
        "binance_api_secret",
        "dashboard_token",
        "runtime_control_token",
        "api_key",
        "api_secret",
        "token",
        "access_token",
        "refresh_token",
        "password",
    }
)


def is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return key.lower() in SENSITIVE_KEYS


def redact_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            key: REDACTED_VALUE if is_sensitive_key(key) else redact_sensitive(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_sensitive(item) for item in obj)
    if isinstance(obj, Decimal):
        return obj
    return obj


def journal_json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(redact_sensitive(value), default=str, ensure_ascii=False)


def redact_json_text(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    return redact_sensitive(parsed)
