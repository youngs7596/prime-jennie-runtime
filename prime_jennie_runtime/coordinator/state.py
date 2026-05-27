"""Coordinator State Hub — read-only 종목 보유 상태 집계 (A1 v1 slice).

design: ../../.ai/designs/2026-05-14-agent-coordinator.md §4 DecisionContext.held_sheets
계획 메모: project_coordinator_a1_slice_2026_05_27.md

목적
----
KIS 계좌 잔고 (``positions`` 테이블) 와 v3 시트 (``position_sheets`` LEFT JOIN
``outcomes`` 의 미청산 행) 를 종목 단위로 합쳐 "현재 들고 있는 종목 + 출처" 한
화면을 제공한다. 출처는 세 가지로 분류한다.

  - ``sheet``    v3 시트로 매수 후 청산 안 됨. KIS 잔고에는 안 잡힌 케이스.
                  (분할 매수 도중 quantity 0 인 경우, 또는 reconcile 갭)
  - ``external`` KIS 잔고에는 있지만 v3 미청산 시트 없음. 사용자 수동 매수 또는
                  v2 잔존 포지션이 여기에 해당.
  - ``mixed``    KIS 잔고 + v3 미청산 시트가 동시에 존재. 정상 보유 경로.

이번 commit (A1 v1) 은 read-only. 활용 (Scout/quant 점수 조정, strategy/engine
차단 룰) 은 다음 commit 에서 별도로. 여기서는 데이터 노출만 한다.

Stage 2+ 에서 queued_sheets / recent_exits / macro_state 등 DecisionContext
전체로 확장할 예정.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


HeldSource = Literal["sheet", "external", "mixed"]


class HeldPositionSummary(BaseModel):
    """종목 단위 보유 요약 — KIS 잔고와 v3 미청산 시트의 합집합."""

    ticker: str
    name: str | None = Field(
        default=None,
        description="positions.stock_name. KIS 잔고에 없고 sheet 만 있으면 None",
    )
    quantity: int = Field(
        default=0,
        ge=0,
        description="KIS 잔고 수량. sheet-only (미체결) 면 0",
    )
    average_buy_price: int | None = Field(
        default=None,
        description="KIS 평균 매입가. sheet-only 면 None",
    )
    sector_group: str | None = None
    source: HeldSource = Field(description="sheet / external / mixed")
    open_sheet_ids: list[str] = Field(
        default_factory=list,
        description="해당 ticker 의 미청산 시트 id 목록 (mixed/sheet 케이스)",
    )


# ─────────────────────────────────────────────────────────────────────
# DB 조회 헬퍼 — context_builder.py 의 _fetch_* 패턴과 동일 (fail-open)
# ─────────────────────────────────────────────────────────────────────


_SQL_POSITIONS_ALL = (
    "SELECT stock_code, stock_name, quantity, average_buy_price, sector_group "
    "FROM positions "
    "WHERE quantity > 0 "
    "ORDER BY stock_code"
)

_SQL_POSITIONS_ONE = (
    "SELECT stock_code, stock_name, quantity, average_buy_price, sector_group "
    "FROM positions "
    "WHERE quantity > 0 AND stock_code = :ticker"
)

# outcomes 행이 아예 없거나 (LEFT JOIN 결과 NULL) closed_at 이 비어 있으면 미청산.
_SQL_OPEN_SHEETS_ALL = (
    "SELECT ps.sheet_id, ps.ticker "
    "FROM position_sheets ps "
    "LEFT JOIN outcomes o ON ps.sheet_id = o.sheet_id "
    "WHERE o.closed_at IS NULL "
    "ORDER BY ps.ticker, ps.generated_at"
)

_SQL_OPEN_SHEETS_ONE = (
    "SELECT ps.sheet_id, ps.ticker "
    "FROM position_sheets ps "
    "LEFT JOIN outcomes o ON ps.sheet_id = o.sheet_id "
    "WHERE o.closed_at IS NULL AND ps.ticker = :ticker "
    "ORDER BY ps.generated_at"
)


async def _fetch_positions(
    engine: AsyncEngine | None,
    *,
    ticker: str | None,
) -> dict[str, dict]:
    """positions 테이블 → {ticker: row dict}. fail-open 시 빈 dict."""
    if engine is None:
        return {}
    try:
        from sqlalchemy import text

        sql = text(_SQL_POSITIONS_ONE if ticker else _SQL_POSITIONS_ALL)
        params = {"ticker": ticker} if ticker else {}
        async with engine.connect() as conn:
            result = await conn.execute(sql, params)
            rows = result.mappings().all()
        return {row["stock_code"]: dict(row) for row in rows}
    except Exception:
        logger.exception("positions fetch failed — using empty map (fail-open)")
        return {}


async def _fetch_open_sheets(
    engine: AsyncEngine | None,
    *,
    ticker: str | None,
) -> dict[str, list[str]]:
    """미청산 시트 → {ticker: [sheet_id, ...]}. fail-open 시 빈 dict."""
    if engine is None:
        return {}
    try:
        from sqlalchemy import text

        sql = text(_SQL_OPEN_SHEETS_ONE if ticker else _SQL_OPEN_SHEETS_ALL)
        params = {"ticker": ticker} if ticker else {}
        async with engine.connect() as conn:
            result = await conn.execute(sql, params)
            rows = result.mappings().all()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["ticker"], []).append(row["sheet_id"])
        return out
    except Exception:
        logger.exception("open_sheets fetch failed — using empty map (fail-open)")
        return {}


# ─────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────


async def get_held_positions(
    engine: AsyncEngine | None,
    *,
    ticker: str | None = None,
) -> list[HeldPositionSummary]:
    """KIS positions + 미청산 시트를 종목 단위로 합친 요약 리스트.

    Parameters
    ----------
    engine:
        SQLAlchemy ``AsyncEngine``. None 이면 빈 list (fail-open) — Scout / 다른
        consumer 가 engine 미주입 환경에서도 안전하게 호출 가능.
    ticker:
        지정 시 해당 ticker 만 반환. None 이면 보유 종목 전체.

    Returns
    -------
    list[HeldPositionSummary]
        ticker 사전순 정렬. 양쪽 모두 비어 있으면 빈 list.
    """
    positions = await _fetch_positions(engine, ticker=ticker)
    open_sheets = await _fetch_open_sheets(engine, ticker=ticker)

    all_tickers = sorted(set(positions) | set(open_sheets))
    summaries: list[HeldPositionSummary] = []
    for tk in all_tickers:
        pos = positions.get(tk)
        sheets = open_sheets.get(tk, [])
        if pos is not None and sheets:
            source: HeldSource = "mixed"
        elif pos is not None:
            source = "external"
        else:
            source = "sheet"

        summaries.append(
            HeldPositionSummary(
                ticker=tk,
                name=pos["stock_name"] if pos else None,
                quantity=int(pos["quantity"]) if pos else 0,
                average_buy_price=int(pos["average_buy_price"]) if pos else None,
                sector_group=pos["sector_group"] if pos else None,
                source=source,
                open_sheet_ids=list(sheets),
            )
        )
    return summaries


__all__ = [
    "HeldPositionSummary",
    "HeldSource",
    "get_held_positions",
]
