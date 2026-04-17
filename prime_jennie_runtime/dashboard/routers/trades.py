"""Trades API — 거래 기록 조회.

v2 원본: `prime_jennie/services/dashboard/routers/trades.py`
v3 매핑: v2 `trade_logs` 테이블 → v3 `executions` (+ `outcomes` 조인으로 pnl 보강).

v3 필드 매핑 (v2 → v3):
  trade_type      ← executions.side (buy/sell → BUY/SELL)
  stock_code      ← position_sheets.ticker (sheet_id 조인)
  quantity        ← executions.qty
  price           ← executions.price
  total_amount    ← price * qty
  timestamp       ← executions.executed_at
  profit_pct      ← outcomes.pnl_pct (SELL 에서만)
  profit_amount   ← outcomes.pnl_krw
  strategy_signal ← position_sheets.strategy_tag

v2-only 필드는 반환 스키마에서 제거 (stock_name/reason/llm_score/market_regime 등):
control-ui 계약은 별도 스펙에서 재정의 예정.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session

router = APIRouter(prefix="/trades", tags=["trades"])


class TradeRecord(BaseModel):
    execution_id: int
    sheet_id: str
    ticker: str | None = None
    strategy_tag: str | None = None
    trade_type: str  # "BUY" | "SELL"
    quantity: int
    price: float
    total_amount: float
    slippage_bps: float | None = None
    profit_pct: float | None = None
    profit_amount: float | None = None
    timestamp: datetime


@router.get("/recent", response_model=list[TradeRecord])
async def get_recent_trades(
    days: int = 7,
    session: AsyncSession = Depends(get_session),
) -> list[TradeRecord]:
    """최근 `days` 일 내 executions + outcomes 조인."""
    since = datetime.now(UTC) - timedelta(days=max(1, days))
    result = await session.execute(
        text(
            "SELECT e.execution_id, e.sheet_id, e.side, e.price, e.qty, e.executed_at, "
            "e.slippage_bps, ps.ticker, ps.strategy_tag, o.pnl_pct, o.pnl_krw "
            "FROM executions e "
            "LEFT JOIN position_sheets ps ON ps.sheet_id = e.sheet_id "
            "LEFT JOIN outcomes o ON o.sheet_id = e.sheet_id "
            "WHERE e.executed_at >= :since "
            "ORDER BY e.executed_at DESC"
        ),
        {"since": since},
    )
    out: list[TradeRecord] = []
    for row in result.mappings().all():
        price = float(row["price"])
        qty = int(row["qty"])
        side = str(row["side"]).upper()
        out.append(
            TradeRecord(
                execution_id=row["execution_id"],
                sheet_id=row["sheet_id"],
                ticker=row["ticker"],
                strategy_tag=row["strategy_tag"],
                trade_type=side,
                quantity=qty,
                price=price,
                total_amount=price * qty,
                slippage_bps=float(row["slippage_bps"])
                if row["slippage_bps"] is not None
                else None,
                profit_pct=float(row["pnl_pct"])
                if side == "SELL" and row["pnl_pct"] is not None
                else None,
                profit_amount=float(row["pnl_krw"])
                if side == "SELL" and row["pnl_krw"] is not None
                else None,
                timestamp=row["executed_at"],
            )
        )
    return out
