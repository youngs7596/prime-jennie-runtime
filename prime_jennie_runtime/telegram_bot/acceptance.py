"""추천 수락 매수 (/accept) 헬퍼 — 시나리오 B 설계 (2026-06-12).

흐름: slow-loop 가 남긴 추천 번호 매핑 (``v3:reco:{KST날짜}``) 으로 시트를
찾고, 시장가 기준 확정 수량을 계산해 echo, "확인" 까지 그 수량을 Redis 에
보존한다 (10분 TTL). 확인 시점 재계산 없음 — 사용자가 본 숫자 그대로
approved_buy payload 로 나간다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from prime_jennie_runtime.position_sheet.schema import PositionSheet

from .control import ACCEPT_PENDING_TTL_SEC, KEY_ACCEPT_PENDING_PREFIX

logger = logging.getLogger(__name__)


async def load_sheet(pool: Any, sheet_id: str) -> PositionSheet | None:
    """position_sheets 의 sheet_json 으로 시트 복원. 없거나 손상 시 None."""
    try:
        row = await pool.fetchrow(
            "SELECT sheet_json FROM position_sheets WHERE sheet_id = $1", sheet_id
        )
    except Exception:
        logger.exception("accept sheet load failed sheet_id=%s", sheet_id)
        return None
    if row is None:
        return None
    raw = row["sheet_json"]
    try:
        data = json.loads(raw) if isinstance(raw, str | bytes) else raw
        return PositionSheet.model_validate(data)
    except Exception:
        logger.exception("accept sheet_json invalid sheet_id=%s", sheet_id)
        return None


def compute_accept_quantity(
    *,
    total_asset: int,
    cash_balance: int,
    price: float,
    sheet: PositionSheet,
) -> tuple[int, int]:
    """(수량, 투입 금액) — fast-loop 자동 진입 수량 계산과 같은 식.

    intraday throttle 배수만 제외 (자동 진입의 시장 급변 감속 장치 — 수락
    매수는 사람이 그 시점에 직접 판단하므로 적용하지 않는다).
    """
    if price <= 0:
        return 0, 0
    caps = [
        int(total_asset * sheet.size.final_pct),
        sheet.size.max_notional_krw,
        cash_balance,
    ]
    if sheet.size.max_notional_pct is not None:
        caps.append(int(total_asset * sheet.size.max_notional_pct))
    notional = min(caps)
    qty = int(notional // price)
    return max(qty, 0), notional


def _pending_key(chat_id: str) -> str:
    return f"{KEY_ACCEPT_PENDING_PREFIX}{chat_id}"


async def store_pending_accept(
    redis_client: Any,
    chat_id: str,
    *,
    number: str,
    sheet_id: str,
    ticker: str,
    quantity: int,
) -> None:
    """echo 시점의 확정 수량 보존 — "확인" 까지 10분 유효."""
    payload = json.dumps(
        {"number": number, "sheet_id": sheet_id, "ticker": ticker, "quantity": quantity}
    )
    await redis_client.set(_pending_key(chat_id), payload, ex=ACCEPT_PENDING_TTL_SEC)


async def load_pending_accept(redis_client: Any, chat_id: str) -> dict[str, Any] | None:
    raw = await redis_client.get(_pending_key(chat_id))
    if not raw:
        return None
    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def clear_pending_accept(redis_client: Any, chat_id: str) -> None:
    await redis_client.delete(_pending_key(chat_id))


__all__ = [
    "clear_pending_accept",
    "compute_accept_quantity",
    "load_pending_accept",
    "load_sheet",
    "store_pending_accept",
]
