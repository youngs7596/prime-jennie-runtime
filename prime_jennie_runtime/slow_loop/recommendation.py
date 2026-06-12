"""추천 발송 — 발행된 시트를 텔레그램 추천 요약으로 (시나리오 B §3-2).

slow-loop 가 시트를 position_sheets 에 영속한 직후 호출. 두 가지를 남긴다:

1. **번호 ↔ 시트 매핑** — Redis HSET ``v3:reco:{KST날짜}`` (당일 TTL).
   텔레그램 봇의 ``/accept 번호`` 가 이 매핑으로 시트를 찾는다. 번호는
   같은 날 안에서 누적 (ad-hoc 재실행 시 기존 번호 뒤에 이어 붙음).
2. **추천 요약 1통** — ``v3:notifications`` 의 GenericAlert (kind=alert).
   기존 dispatcher/formatter 가 그대로 처리하므로 새 kind 불필요.

실패해도 슬로우루프 본체에 영향을 주면 안 된다 — 호출부 (pipeline) 가
best-effort try/except 로 감싼다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, text

from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_NOTIFICATIONS,
    TypedStreamPublisher,
)
from prime_jennie_runtime.position_sheet.schema import PositionSheet
from prime_jennie_runtime.telegram_bot.control import reco_key

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 매핑 TTL — 추천은 당일 장 마감까지만 의미 있지만, 디버깅 여지로 24h.
RECO_TTL_SEC = 86_400

_NAMES_SQL = text(
    "SELECT stock_code, stock_name FROM stock_masters WHERE stock_code IN :codes"
).bindparams(bindparam("codes", expanding=True))


async def _load_names(db_engine: Any, tickers: list[str]) -> dict[str, str]:
    """종목명 lookup — 실패하면 빈 dict (ticker 로 fallback)."""
    if db_engine is None or not tickers:
        return {}
    try:
        async with db_engine.connect() as conn:
            rows = (await conn.execute(_NAMES_SQL, {"codes": tickers})).all()
        return {str(r[0]): str(r[1]) for r in rows}
    except Exception:
        logger.exception("recommendation name lookup failed")
        return {}


async def announce_recommendations(
    redis_client: Any,
    db_engine: Any,
    sheets: list[PositionSheet],
    *,
    as_of_dt: datetime,
) -> int:
    """추천 매핑 저장 + 텔레그램 요약 발송. 발송한 추천 건수 반환."""
    if not sheets:
        return 0

    kst_date = as_of_dt.astimezone(KST).strftime("%Y-%m-%d")
    key = reco_key(kst_date)
    start = int(await redis_client.hlen(key))
    names = await _load_names(db_engine, [s.ticker for s in sheets])

    mapping: dict[str, str] = {}
    lines: list[str] = []
    for i, sheet in enumerate(sheets, start=start + 1):
        mapping[str(i)] = sheet.sheet_id
        name = names.get(sheet.ticker, sheet.ticker)
        conviction = sheet.provenance.conviction
        conv_note = f" · 확신도 {conviction:.2f}" if conviction is not None else ""
        valid_kst = sheet.valid_until.astimezone(KST).strftime("%H:%M")
        lines.append(
            f"{i}. <b>{name}({sheet.ticker})</b> · {sheet.strategy_tag}{conv_note}"
            f" · ~{valid_kst} 유효"
        )

    await redis_client.hset(key, mapping=mapping)
    await redis_client.expire(key, RECO_TTL_SEC)

    publisher: TypedStreamPublisher[GenericAlertNotification] = TypedStreamPublisher(
        redis_client, STREAM_NOTIFICATIONS, GenericAlertNotification
    )
    body = (
        "\n".join(lines)
        + "\n\n수락: <code>/accept 번호</code> — 수량·금액 해석을 본 뒤 확인으로 매수"
    )
    await publisher.publish(
        GenericAlertNotification(
            severity="info",
            title=f"오늘의 추천 {len(sheets)}건",
            body=body,
            ts=as_of_dt,
            metadata={"reco_key": key, "numbers": list(mapping.keys())},
        )
    )
    logger.info("recommendations announced: %d sheets (key=%s)", len(sheets), key)
    return len(sheets)


__all__ = ["RECO_TTL_SEC", "announce_recommendations"]
