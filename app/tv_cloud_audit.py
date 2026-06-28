from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .tv_observation import aggregate_level
from .tv_sandbox import is_tv_execution_row

if TYPE_CHECKING:
    from .config import Settings
    from .storage import TradeJournalStore


def _count_duplicate_signals(rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Count extra occurrences beyond the first for each repeated signal_id."""
    counter: Counter[str] = Counter()
    for row in rows:
        signal_id = str(row.get("signal_id") or "").strip()
        if not signal_id:
            continue
        counter[signal_id] += 1
    duplicate_ids = [sid for sid, count in counter.items() if count > 1]
    duplicate_count = sum(count - 1 for count in counter.values() if count > 1)
    return duplicate_count, sorted(duplicate_ids)


def _is_runtime_locked_row(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "")
    skip = str(row.get("skip_reason") or "")
    return status == "blocked_by_runtime_lock" or skip == "runtime_locked"


def _recent_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "signal_id": row.get("signal_id"),
        "symbol": row.get("symbol"),
        "status": row.get("status"),
        "skip_reason": row.get("skip_reason"),
    }


def build_tv_cloud_audit(
    settings: Settings,
    journal_store: TradeJournalStore,
    *,
    hours: int | None = None,
) -> dict[str, Any]:
    window_hours = hours if hours is not None else settings.tv_cloud_audit_window_hours
    window_hours = max(1, min(int(window_hours), 168))
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    since_iso = since.isoformat()

    all_rows = journal_store.list_executions_since(since_iso, limit=1000)
    tv_rows = [row for row in all_rows if is_tv_execution_row(row, settings)]

    duplicate_count, duplicate_ids = _count_duplicate_signals(tv_rows)
    payload_invalid_count = sum(
        1 for row in tv_rows if str(row.get("skip_reason") or "") == "tv_payload_invalid"
    )
    runtime_locked_count = sum(1 for row in tv_rows if _is_runtime_locked_row(row))
    protected_count = sum(1 for row in tv_rows if str(row.get("status") or "") == "protected")
    failed_count = sum(
        1
        for row in tv_rows
        if str(row.get("status") or "") in {"failed", "protection_failed"}
    )

    last_row = tv_rows[0] if tv_rows else None
    summary: dict[str, Any] = {
        "total_cloud_signals": len(tv_rows),
        "duplicate_signal_count": duplicate_count,
        "unauthorized_count": 0,
        "payload_invalid_count": payload_invalid_count,
        "runtime_locked_count": runtime_locked_count,
        "protected_count": protected_count,
        "failed_count": failed_count,
        "last_signal_id": last_row.get("signal_id") if last_row else None,
        "last_status": last_row.get("status") if last_row else None,
        "last_symbol": last_row.get("symbol") if last_row else None,
        "last_time": last_row.get("created_at") if last_row else None,
    }

    recent = [_recent_row_payload(row) for row in tv_rows[:20]]
    checks: list[dict[str, str]] = []

    if not settings.tv_cloud_audit_enabled:
        checks.append(
            {
                "name": "tv_cloud_audit_enabled",
                "level": "WARN",
                "message": "TV_CLOUD_AUDIT_ENABLED=false，云端 Alert 审计未启用",
            }
        )
        return {
            "level": aggregate_level(checks),
            "window_hours": window_hours,
            "summary": summary,
            "checks": checks,
            "recent": recent,
        }

    checks.append(
        {
            "name": "tv_cloud_audit_enabled",
            "level": "OK",
            "message": f"TV 云端 Alert 审计已启用，窗口 {window_hours}h",
        }
    )

    if duplicate_count >= settings.tv_cloud_duplicate_signal_warn:
        checks.append(
            {
                "name": "tv_cloud_duplicate_signal",
                "level": "WARN",
                "message": (
                    f"窗口内重复 signal_id {duplicate_count} 次 "
                    f"(阈值>={settings.tv_cloud_duplicate_signal_warn})"
                    + (f": {', '.join(duplicate_ids[:5])}" if duplicate_ids else "")
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "tv_cloud_duplicate_signal",
                "level": "OK",
                "message": f"窗口内无重复 signal_id 超阈值 (count={duplicate_count})",
            }
        )

    if payload_invalid_count >= settings.tv_cloud_payload_invalid_warn:
        checks.append(
            {
                "name": "tv_cloud_payload_invalid",
                "level": "WARN",
                "message": (
                    f"窗口内 tv_payload_invalid {payload_invalid_count} 次 "
                    f"(阈值>={settings.tv_cloud_payload_invalid_warn})"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "tv_cloud_payload_invalid",
                "level": "OK",
                "message": f"窗口内 tv_payload_invalid {payload_invalid_count} 次",
            }
        )

    if runtime_locked_count >= settings.tv_cloud_runtime_lock_warn:
        checks.append(
            {
                "name": "tv_cloud_runtime_locked_many",
                "level": "WARN",
                "message": (
                    f"窗口内 Runtime Lock 拦截 {runtime_locked_count} 次 "
                    f"(阈值>={settings.tv_cloud_runtime_lock_warn})"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "tv_cloud_runtime_locked_many",
                "level": "OK",
                "message": f"窗口内 Runtime Lock 拦截 {runtime_locked_count} 次",
            }
        )

    checks.append(
        {
            "name": "tv_cloud_unauthorized",
            "level": "INFO",
            "message": (
                "401 未授权请求未持久化到 journal，unauthorized_count 当前为 0；"
                "请结合服务器 access log 或后续版本内存计数观察"
            ),
        }
    )

    return {
        "level": aggregate_level(checks),
        "window_hours": window_hours,
        "summary": summary,
        "checks": checks,
        "recent": recent,
    }


_CLOUD_AUDIT_ALERT_TITLES: dict[str, str] = {
    "tv_cloud_duplicate_signal": "TradingView 云端重复 signal_id",
    "tv_cloud_payload_invalid": "TradingView 云端 payload 无效",
    "tv_cloud_unauthorized": "TradingView 云端未授权请求",
    "tv_cloud_runtime_locked_many": "TradingView 云端 Runtime Lock 拦截过多",
    "tv_cloud_audit_enabled": "TradingView 云端审计未启用",
}


def alerts_from_tv_cloud_audit(audit: dict[str, Any], *, created_at: str | None) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    summary = audit.get("summary") or {}
    for check in audit.get("checks") or []:
        level = check.get("level", "OK")
        if level not in {"WARN", "ERROR"}:
            continue
        name = str(check.get("name") or "tv_cloud_audit")
        alert_type = name
        if name == "tv_cloud_runtime_locked_many":
            alert_type = "tv_cloud_runtime_locked_many"
        alerts.append(
            {
                "id": f"tv-cloud-audit-{name}",
                "source": "tv_cloud_audit",
                "level": level,
                "type": alert_type,
                "title": _CLOUD_AUDIT_ALERT_TITLES.get(name, name),
                "message": str(check.get("message") or ""),
                "symbol": summary.get("last_symbol"),
                "status": summary.get("last_status"),
                "reason": name,
                "created_at": created_at or summary.get("last_time"),
            }
        )
    return alerts
