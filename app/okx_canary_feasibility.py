from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import requests

from .exchanges.okx_sizing import OkxInstrumentMeta, compute_minimal_contract_sz, parse_instrument_row

if TYPE_CHECKING:
    from .config import Settings


@dataclass(frozen=True)
class OkxCanaryFeasibility:
    feasible: bool
    inst_id: str
    mark_price: Decimal
    meta: OkxInstrumentMeta
    sz: Decimal | None
    required_notional: Decimal | None
    required_margin: Decimal | None
    configured_margin_usdt: Decimal
    configured_max_notional: Decimal
    leverage: Decimal
    rejection_reason: str | None = None
    sizing_error: str | None = None


def evaluate_okx_canary_feasibility(
    *,
    inst_id: str,
    meta: OkxInstrumentMeta,
    mark_price: Decimal,
    margin_usdt: Decimal,
    max_notional_usdt: Decimal,
    leverage: Decimal | int = 1,
) -> OkxCanaryFeasibility:
    lev = Decimal(str(leverage))
    if lev <= 0:
        lev = Decimal("1")

    try:
        sz, notional, required_margin = compute_minimal_contract_sz(
            meta,
            mark_price=mark_price,
            margin_usdt=margin_usdt,
            leverage=lev,
        )
    except ValueError as exc:
        return OkxCanaryFeasibility(
            feasible=False,
            inst_id=inst_id,
            mark_price=mark_price,
            meta=meta,
            sz=None,
            required_notional=None,
            required_margin=None,
            configured_margin_usdt=margin_usdt,
            configured_max_notional=max_notional_usdt,
            leverage=lev,
            rejection_reason="sizing_infeasible",
            sizing_error=str(exc),
        )

    if notional > max_notional_usdt:
        return OkxCanaryFeasibility(
            feasible=False,
            inst_id=inst_id,
            mark_price=mark_price,
            meta=meta,
            sz=sz,
            required_notional=notional,
            required_margin=required_margin,
            configured_margin_usdt=margin_usdt,
            configured_max_notional=max_notional_usdt,
            leverage=lev,
            rejection_reason="required_notional_exceeds_max",
            sizing_error=(
                f"required_notional={notional} > configured_max_notional={max_notional_usdt}"
            ),
        )

    return OkxCanaryFeasibility(
        feasible=True,
        inst_id=inst_id,
        mark_price=mark_price,
        meta=meta,
        sz=sz,
        required_notional=notional,
        required_margin=required_margin,
        configured_margin_usdt=margin_usdt,
        configured_max_notional=max_notional_usdt,
        leverage=lev,
    )


def fetch_public_instrument(settings: Settings, inst_id: str) -> OkxInstrumentMeta:
    url = f"{settings.okx_base_url.rstrip('/')}/api/v5/public/instruments"
    resp = requests.get(
        url,
        params={
            "instType": settings.okx_inst_type,
            "instId": inst_id.upper(),
        },
        timeout=settings.request_timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OKX public instruments HTTP {resp.status_code}: {resp.text}")
    payload = resp.json()
    code = str(payload.get("code", ""))
    if code not in {"0", ""}:
        raise RuntimeError(f"OKX public instruments error code={code} msg={payload.get('msg')}")
    rows = payload.get("data") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Instrument metadata not found for instId={inst_id}")
    return parse_instrument_row(rows[0])


def fetch_public_mark_price(settings: Settings, inst_id: str) -> Decimal:
    url = f"{settings.okx_base_url.rstrip('/')}/api/v5/market/ticker"
    resp = requests.get(
        url,
        params={"instId": inst_id.upper()},
        timeout=settings.request_timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OKX public ticker HTTP {resp.status_code}: {resp.text}")
    payload = resp.json()
    code = str(payload.get("code", ""))
    if code not in {"0", ""}:
        raise RuntimeError(f"OKX public ticker error code={code} msg={payload.get('msg')}")
    rows = payload.get("data") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Ticker not found for instId={inst_id}")
    last = rows[0].get("last") or rows[0].get("markPx")
    if last in {None, ""}:
        raise RuntimeError(f"Mark price missing for instId={inst_id}")
    return Decimal(str(last))


def format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return format(value, "f")


def print_feasibility_rejection(result: OkxCanaryFeasibility, *, price_label: str = "mark_price") -> None:
    print("REJECTED: OKX canary payload not generated", file=sys.stderr)
    print(f"instId={result.inst_id}", file=sys.stderr)
    print(f"{price_label}={format_decimal(result.mark_price)}", file=sys.stderr)
    print(f"minSz={format_decimal(result.meta.min_sz)}", file=sys.stderr)
    print(f"lotSz={format_decimal(result.meta.lot_sz)}", file=sys.stderr)
    print(f"ctVal={format_decimal(result.meta.ct_val)}", file=sys.stderr)
    print(f"required_notional={format_decimal(result.required_notional)}", file=sys.stderr)
    print(f"required_margin={format_decimal(result.required_margin)}", file=sys.stderr)
    print(f"configured_margin_usdt={format_decimal(result.configured_margin_usdt)}", file=sys.stderr)
    print(
        f"configured_max_notional={format_decimal(result.configured_max_notional)}",
        file=sys.stderr,
    )
    if result.rejection_reason:
        print(f"rejection_reason={result.rejection_reason}", file=sys.stderr)
    if result.sizing_error:
        print(f"detail={result.sizing_error}", file=sys.stderr)


def print_feasibility_report(result: OkxCanaryFeasibility) -> None:
    status = "SUITABLE" if result.feasible else "NOT_SUITABLE"
    print(f"instId={result.inst_id}")
    print(f"mark_price={format_decimal(result.mark_price)}")
    print(f"minSz={format_decimal(result.meta.min_sz)}")
    print(f"lotSz={format_decimal(result.meta.lot_sz)}")
    print(f"ctVal={format_decimal(result.meta.ct_val)}")
    print(f"required_sz={format_decimal(result.sz)}")
    print(f"required_notional={format_decimal(result.required_notional)}")
    print(f"required_margin={format_decimal(result.required_margin)}")
    print(f"configured_margin_usdt={format_decimal(result.configured_margin_usdt)}")
    print(f"configured_max_notional={format_decimal(result.configured_max_notional)}")
    print(f"leverage={format_decimal(result.leverage)}")
    print(f"canary_feasible={str(result.feasible).lower()}")
    print(f"canary_suitability={status}")
    if not result.feasible:
        if result.rejection_reason:
            print(f"rejection_reason={result.rejection_reason}")
        if result.sizing_error:
            print(f"detail={result.sizing_error}")


def feasibility_from_settings(
    settings: Settings,
    *,
    inst_id: str,
    meta: OkxInstrumentMeta,
    mark_price: Decimal,
    margin_usdt: Decimal | None = None,
    max_notional_usdt: Decimal | None = None,
    leverage: int = 1,
) -> OkxCanaryFeasibility:
    margin = margin_usdt if margin_usdt is not None else Decimal(str(settings.okx_max_margin_usdt))
    max_notional = (
        max_notional_usdt
        if max_notional_usdt is not None
        else Decimal(str(settings.okx_max_position_notional_usdt))
    )
    return evaluate_okx_canary_feasibility(
        inst_id=inst_id,
        meta=meta,
        mark_price=mark_price,
        margin_usdt=margin,
        max_notional_usdt=max_notional,
        leverage=leverage,
    )
