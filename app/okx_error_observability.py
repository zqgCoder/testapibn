from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

OKX_CANARY_STATUS_OPEN_FAILED = "okx_canary_open_failed"
OKX_CANARY_STATUS_CLOSE_FAILED = "okx_canary_close_failed"
OKX_CANARY_STATUS_RECONCILE_FAILED = "okx_canary_reconcile_failed"


def okx_error_observability_fields(exc: Any) -> dict[str, Any]:
    return {
        "okx_code": getattr(exc, "code", None),
        "okx_msg": getattr(exc, "msg", None),
        "okx_scode": getattr(exc, "s_code", None),
        "okx_smsg": getattr(exc, "s_msg", None),
        "request_path": getattr(exc, "request_path", None),
        "http_status": getattr(exc, "http_status", None),
        "method": getattr(exc, "method", None),
    }


def build_okx_error_summary(
    *,
    error_stage: str,
    exc: Exception,
    inst_id: str | None = None,
    td_mode: str | None = None,
    side: str | None = None,
    sz: Decimal | str | None = None,
    cl_ord_id: str | None = None,
) -> dict[str, Any]:
    if hasattr(exc, "s_code") or hasattr(exc, "code"):
        fields = okx_error_observability_fields(exc)
        code_part = fields.get("okx_scode") or fields.get("okx_code") or "unknown"
        msg_part = fields.get("okx_smsg") or fields.get("okx_msg") or str(exc)
        error_summary = f"{error_stage}: {code_part} {msg_part}".strip()
    else:
        fields = {
            "okx_code": None,
            "okx_msg": None,
            "okx_scode": None,
            "okx_smsg": None,
            "request_path": None,
            "http_status": None,
            "method": None,
        }
        error_summary = f"{error_stage}: {type(exc).__name__}: {exc}"

    payload: dict[str, Any] = {
        "error_stage": error_stage,
        "error_summary": error_summary[:2000],
        **fields,
    }
    if inst_id is not None:
        payload["instId"] = inst_id
    if td_mode is not None:
        payload["tdMode"] = td_mode
    if side is not None:
        payload["side"] = side
    if sz is not None:
        payload["sz"] = format(sz, "f") if isinstance(sz, Decimal) else str(sz)
    if cl_ord_id is not None:
        payload["clOrdId"] = cl_ord_id
    return payload


def log_okx_order_error(
    *,
    error_stage: str,
    exc: Any,
    inst_id: str,
    td_mode: str,
    side: str | None = None,
    sz: Decimal | str | None = None,
    cl_ord_id: str | None = None,
) -> None:
    logger.error(
        "OKX canary order failed: error_stage=%s instId=%s tdMode=%s side=%s sz=%s clOrdId=%s "
        "okx_code=%s okx_msg=%s okx_scode=%s okx_smsg=%s request_path=%s",
        error_stage,
        inst_id,
        td_mode,
        side,
        format(sz, "f") if isinstance(sz, Decimal) else sz,
        cl_ord_id,
        exc.code,
        exc.msg,
        getattr(exc, "s_code", None),
        getattr(exc, "s_msg", None),
        getattr(exc, "request_path", None),
    )


def extract_okx_error_from_result(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    okx_error = result.get("okx_error")
    if isinstance(okx_error, dict):
        return {
            "error_stage": okx_error.get("error_stage") or result.get("error_stage"),
            "error_summary": okx_error.get("error_summary") or result.get("error_summary"),
            "okx_code": okx_error.get("okx_code"),
            "okx_msg": okx_error.get("okx_msg"),
            "okx_scode": okx_error.get("okx_scode"),
            "okx_smsg": okx_error.get("okx_smsg"),
        }
    return {
        "error_stage": result.get("error_stage"),
        "error_summary": result.get("error_summary"),
        "okx_code": None,
        "okx_msg": None,
        "okx_scode": None,
        "okx_smsg": None,
    }
