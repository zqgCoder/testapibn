from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import Header, HTTPException, Query
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .config import Settings
    from .storage import RuntimeControlStore

logger = logging.getLogger(__name__)


def resolve_runtime_operator(
    operator: str | None = None,
    actor: str | None = None,
    locked_by: str | None = None,
) -> str | None:
    for value in (operator, actor, locked_by):
        if value in {None, ""}:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


class LockRequest(BaseModel):
    reason: str = Field(default="manual lock", max_length=500)
    locked_until: str | None = Field(default=None, description="ISO8601 UTC expiry; null = no expiry")
    operator: str | None = Field(default=None, max_length=120)
    actor: str | None = Field(default=None, max_length=120)
    locked_by: str | None = Field(
        default=None,
        max_length=120,
        description="Optional operator alias; saved to runtime_state.locked_by",
    )

    def resolved_operator(self) -> str | None:
        return resolve_runtime_operator(self.operator, self.actor, self.locked_by)


class UnlockRequest(BaseModel):
    operator: str | None = Field(default=None, max_length=120)
    actor: str | None = Field(default=None, max_length=120)
    locked_by: str | None = Field(default=None, max_length=120)

    def resolved_operator(self) -> str | None:
        return resolve_runtime_operator(self.operator, self.actor, self.locked_by)


class UnlockOnceRequest(BaseModel):
    reason: str = Field(default="one-shot unlock", max_length=500)
    operator: str = Field(default="local-admin", max_length=120)
    ttl_seconds: int = Field(default=900, ge=30, le=3600)


class RuntimeControl:
    def __init__(self, settings: Settings, store: RuntimeControlStore) -> None:
        self.settings = settings
        self.store = store

    @staticmethod
    def summary_dict(state: dict) -> dict[str, Any]:
        return {
            "locked": bool(state.get("locked")),
            "effective_locked": bool(state.get("effective_locked", state.get("locked"))),
            "reason": state.get("reason"),
            "locked_until": state.get("locked_until"),
            "locked_by": state.get("locked_by"),
            "locked_at": state.get("locked_at"),
        }

    @staticmethod
    def one_shot_payload(state: dict) -> dict[str, Any]:
        return {
            "enabled": bool(state.get("one_shot_enabled")),
            "remaining": int(state.get("one_shot_remaining") or 0),
            "reason": state.get("one_shot_reason"),
            "operator": state.get("one_shot_operator"),
            "started_at": state.get("one_shot_started_at"),
            "expires_at": state.get("one_shot_expires_at"),
            "consumed_by_signal_id": state.get("one_shot_consumed_by_signal_id"),
            "consumed_at": state.get("one_shot_consumed_at"),
        }

    @staticmethod
    def _parse_locked_until(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"locked_until must be ISO8601 datetime: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _one_shot_active(state: dict) -> bool:
        return (
            bool(state.get("one_shot_enabled"))
            and int(state.get("one_shot_remaining") or 0) > 0
            and not state.get("one_shot_consumed_at")
        )

    def _with_effective_locked(self, state: dict) -> dict:
        effective_locked = bool(state.get("locked"))
        if self._one_shot_active(state):
            effective_locked = False
        return {**state, "effective_locked": effective_locked}

    def _maybe_auto_expire(self, state: dict) -> dict:
        if not state.get("locked"):
            return state
        locked_until = state.get("locked_until")
        if not locked_until:
            return state
        try:
            until = self._parse_locked_until(locked_until)
        except ValueError:
            return state
        if until is None:
            return state
        now = datetime.now(timezone.utc)
        if now < until:
            return state
        previous_reason = state.get("reason")
        self.store.set_unlocked(actor="system")
        self.store.append_event(
            action="auto_expire",
            reason=previous_reason,
            locked_until=locked_until,
            actor="system",
        )
        logger.info("Runtime lock auto-expired: locked_until=%s reason=%s", locked_until, previous_reason)
        return self.store.get_state()

    def _maybe_expire_one_shot(self, state: dict) -> dict:
        if not self._one_shot_active(state):
            return state
        expires_at = state.get("one_shot_expires_at")
        if not expires_at:
            return state
        try:
            until = self._parse_locked_until(expires_at)
        except ValueError:
            return state
        if until is None:
            return state
        now = datetime.now(timezone.utc)
        if now < until:
            return state
        previous_reason = state.get("one_shot_reason")
        self.store.expire_one_shot_and_lock(lock_reason="one-shot expired", actor="system")
        self.store.append_event(
            action="one_shot_expired",
            reason=f"one-shot expired (previous_reason={previous_reason or '-'})",
            locked_until=expires_at,
            actor="system",
        )
        logger.info("Runtime one-shot expired: expires_at=%s reason=%s", expires_at, previous_reason)
        return self.store.get_state()

    def effective_state(self) -> dict:
        state = self.store.get_state()
        if not self.settings.runtime_control_enabled:
            return self._with_effective_locked({**state, "locked": False})
        state = self._maybe_auto_expire(state)
        state = self._maybe_expire_one_shot(state)
        return self._with_effective_locked(state)

    def is_execution_blocked(self) -> tuple[bool, dict[str, Any]]:
        if not self.settings.runtime_control_enabled:
            return False, {}
        state = self.effective_state()
        if not state.get("effective_locked"):
            return False, {}
        return True, self.summary_dict(state)

    def status_payload(self) -> dict[str, Any]:
        state = self.effective_state()
        payload = {
            "enabled": self.settings.runtime_control_enabled,
            **self.summary_dict(state),
            "one_shot": self.one_shot_payload(state),
            "updated_at": state.get("updated_at"),
        }
        return payload

    def lock(
        self,
        *,
        reason: str,
        locked_until: str | None,
        operator: str | None = None,
        actor: str | None = None,
        locked_by: str | None = None,
    ) -> dict[str, Any]:
        resolved = resolve_runtime_operator(operator, actor, locked_by)
        if locked_until:
            parsed = self._parse_locked_until(locked_until)
            if parsed <= datetime.now(timezone.utc):
                raise ValueError("locked_until must be in the future")
            locked_until = parsed.isoformat()
        state = self.store.set_locked(reason=reason, locked_until=locked_until, actor=resolved)
        self.store.append_event(action="lock", reason=reason, locked_until=locked_until, actor=resolved)
        logger.info("Runtime locked: reason=%s locked_until=%s operator=%s", reason, locked_until, resolved)
        return self.summary_dict(self._with_effective_locked(state))

    def unlock(
        self,
        *,
        operator: str | None = None,
        actor: str | None = None,
        locked_by: str | None = None,
    ) -> dict[str, Any]:
        resolved = resolve_runtime_operator(operator, actor, locked_by)
        previous = self.store.get_state()
        state = self.store.set_unlocked(actor=resolved)
        self.store.append_event(
            action="unlock",
            reason=previous.get("reason"),
            locked_until=previous.get("locked_until"),
            actor=resolved,
        )
        logger.info("Runtime unlocked: operator=%s", resolved)
        return self.summary_dict(self._with_effective_locked(state))

    def unlock_once(
        self,
        *,
        reason: str,
        operator: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=int(ttl_seconds))
        started_at = now.isoformat()
        expires_at = expires.isoformat()
        state = self.store.set_one_shot_unlock(
            reason=reason.strip() or "one-shot unlock",
            operator=operator.strip() or "local-admin",
            started_at=started_at,
            expires_at=expires_at,
        )
        self.store.append_event(
            action="unlock_once",
            reason=reason,
            locked_until=expires_at,
            actor=operator,
        )
        logger.info(
            "Runtime one-shot unlock enabled: operator=%s ttl=%ss expires_at=%s reason=%s",
            operator,
            ttl_seconds,
            expires_at,
            reason,
        )
        return self.summary_dict(self._with_effective_locked(state))

    def maybe_consume_one_shot_for_tv_signal(self, signal_id: str, status: str) -> bool:
        if not self.settings.runtime_control_enabled:
            return False
        state = self.store.get_state()
        if not self._one_shot_active(state):
            return False
        safe_signal_id = (signal_id or "unknown").strip() or "unknown"
        safe_status = (status or "unknown").strip() or "unknown"
        lock_reason = f"one-shot consumed by signal_id={safe_signal_id} status={safe_status}"
        before = self.store.get_state()
        self.store.consume_one_shot_and_lock(signal_id=safe_signal_id, lock_reason=lock_reason)
        after = self.store.get_state()
        consumed = (
            after.get("one_shot_consumed_at")
            and after.get("one_shot_consumed_at") != before.get("one_shot_consumed_at")
        )
        if consumed:
            self.store.append_event(
                action="one_shot_consumed",
                reason=lock_reason,
                locked_until=before.get("one_shot_expires_at"),
                actor="system",
            )
            logger.info("Runtime one-shot consumed: %s", lock_reason)
        return bool(consumed)

    def list_events(self, limit: int = 50) -> list[dict]:
        return self.store.list_events(limit=limit)


def _token_matches(expected: str, provided: str | None) -> bool:
    return bool(expected and provided and provided == expected)


def verify_runtime_control_write_token(
    settings: Settings,
    *,
    control_token: str | None,
    header_token: str | None,
) -> None:
    if not settings.runtime_control_enabled:
        raise HTTPException(status_code=404, detail="Runtime Control 未启用")
    if not settings.runtime_control_require_token:
        return
    if not settings.runtime_control_token:
        raise HTTPException(status_code=403, detail="Runtime Control Token 未配置")
    provided = header_token or control_token
    if not _token_matches(settings.runtime_control_token, provided):
        raise HTTPException(status_code=401, detail="Runtime Control Token 无效")


def verify_runtime_read_token(
    settings: Settings,
    *,
    control_token: str | None,
    control_header: str | None,
    dashboard_token: str | None,
    dashboard_header: str | None,
) -> None:
    if not settings.runtime_control_enabled:
        return
    if not settings.runtime_control_require_token:
        return
    runtime_provided = control_header or control_token
    if _token_matches(settings.runtime_control_token, runtime_provided):
        return
    if settings.runtime_status_allow_dashboard_token:
        dashboard_provided = dashboard_header or dashboard_token
        if _token_matches(settings.dashboard_token, dashboard_provided):
            return
    if not settings.runtime_control_token and not settings.dashboard_token:
        raise HTTPException(status_code=403, detail="Runtime 读接口 Token 未配置")
    raise HTTPException(status_code=401, detail="Runtime 读接口 Token 无效")
