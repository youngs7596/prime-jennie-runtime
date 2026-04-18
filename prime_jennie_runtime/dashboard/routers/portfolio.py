"""Portfolio API — 포트폴리오 상태, 보유 종목, 자산 히스토리.

v2 원본: `prime_jennie/services/dashboard/routers/portfolio.py`

v3 매핑:
- 실시간 잔고: v3 KIS Gateway HTTP `/balance` 호출 (`KIS_GATEWAY_URL`)
- 포지션 메타데이터: `position_sheets` (ticker/strategy_tag) + `outcomes` (closed_at IS NULL → 오픈)
- 자산 스냅샷: v3 `daily_prices` 기반 일별 집계는 별도 job 에서 생성 예정.
  현재는 KIS 잔고 단일 소스만 반환 (history 엔드포인트는 빈 배열).
- 성과 요약: `outcomes` 기준 집계 (pnl_pct / pnl_krw).

Redis 실시간 스냅샷 키(v2 `monitoring:live_positions`)는 v3 monitor 포팅 이후 연동.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_redis, get_session

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

logger = logging.getLogger(__name__)


class Position(BaseModel):
    stock_code: str
    stock_name: str | None = None
    quantity: int
    average_buy_price: float
    total_buy_amount: float
    current_price: float | None = None
    current_value: float | None = None
    profit_pct: float | None = None
    strategy_tag: str | None = None


class PortfolioState(BaseModel):
    positions: list[Position]
    cash_balance: int
    total_asset: int
    stock_eval_amount: int
    position_count: int
    timestamp: datetime


class PerformanceSummary(BaseModel):
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_profit: int = 0


class DailySnapshot(BaseModel):
    snapshot_date: datetime
    total_asset: int
    cash_balance: int
    stock_eval_amount: int
    realized_profit_loss: int | None = None


def _kis_gateway_url() -> str:
    return os.getenv("KIS_GATEWAY_URL", "http://localhost:8080")


async def _fetch_kis_balance() -> dict[str, Any] | None:
    """v3 KIS Gateway REST `/api/balance` 호출. 실패 시 None."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_kis_gateway_url()}/api/balance")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.warning("KIS Gateway balance fetch failed", exc_info=True)
        return None


async def _open_sheet_meta(session: AsyncSession) -> dict[str, dict]:
    """outcomes.closed_at IS NULL 인 position_sheets 의 ticker → meta 매핑."""
    result = await session.execute(
        text(
            "SELECT ps.ticker, ps.strategy_tag, ps.sheet_json "
            "FROM position_sheets ps "
            "LEFT JOIN outcomes o ON o.sheet_id = ps.sheet_id "
            "WHERE o.sheet_id IS NULL OR o.closed_at IS NULL"
        )
    )
    meta: dict[str, dict] = {}
    for row in result.mappings().all():
        meta[row["ticker"]] = {
            "strategy_tag": row["strategy_tag"],
            "sheet_json": row["sheet_json"],
        }
    return meta


@router.get("/summary", response_model=PortfolioState)
async def get_summary(session: AsyncSession = Depends(get_session)) -> PortfolioState:
    """포트폴리오 전체 요약. v3 KIS Gateway 실시간 잔고 우선."""
    balance = await _fetch_kis_balance()
    meta = await _open_sheet_meta(session)

    positions: list[Position] = []
    cash = 0
    total = 0
    stock_eval = 0
    if balance is not None:
        cash = int(balance.get("cash_balance", 0))
        total = int(balance.get("total_asset", 0))
        stock_eval = int(balance.get("stock_eval_amount", 0))
        for p in balance.get("positions", []):
            code = p["stock_code"]
            sheet_meta = meta.get(code, {})
            positions.append(
                Position(
                    stock_code=code,
                    stock_name=p.get("stock_name"),
                    quantity=int(p["quantity"]),
                    average_buy_price=float(p["average_buy_price"]),
                    total_buy_amount=float(p["total_buy_amount"]),
                    current_price=p.get("current_price"),
                    current_value=p.get("current_value"),
                    profit_pct=p.get("profit_pct"),
                    strategy_tag=sheet_meta.get("strategy_tag"),
                )
            )

    return PortfolioState(
        positions=positions,
        cash_balance=cash,
        total_asset=total,
        stock_eval_amount=stock_eval,
        position_count=len(positions),
        timestamp=datetime.now(UTC),
    )


@router.get("/live")
async def get_live_positions(
    redis_client: aioredis.Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """monitor 가 캐싱한 실시간 포지션. Redis 비면 KIS Gateway fallback."""
    try:
        raw = await redis_client.get("monitoring:live_positions")
        if raw:
            import json

            return json.loads(raw)
    except Exception:
        logger.debug("Redis live_positions read failed", exc_info=True)

    summary = await get_summary(session)
    return {
        "positions": [p.model_dump() for p in summary.positions],
        "updated_at": summary.timestamp.isoformat(),
    }


@router.get("/history", response_model=list[DailySnapshot])
async def get_history(days: int = 30) -> list[DailySnapshot]:
    """자산 히스토리 — v3 별도 스냅샷 테이블 미구현. 빈 배열 반환 (UI 는 placeholder 처리)."""
    return []


@router.get("/performance", response_model=PerformanceSummary)
async def get_performance(
    days: int = 30,
    session: AsyncSession = Depends(get_session),
) -> PerformanceSummary:
    """`outcomes` 기준 최근 `days` 일 성과 요약 (closed_at 필터)."""
    since = datetime.now(UTC) - timedelta(days=max(1, days))
    result = await session.execute(
        text(
            "SELECT pnl_pct, pnl_krw FROM outcomes "
            "WHERE closed_at IS NOT NULL AND closed_at >= :since"
        ),
        {"since": since},
    )
    rows = result.mappings().all()
    if not rows:
        return PerformanceSummary()

    pnl_pcts = [float(r["pnl_pct"]) for r in rows if r["pnl_pct"] is not None]
    pnl_krws = [float(r["pnl_krw"]) for r in rows if r["pnl_krw"] is not None]
    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p <= 0]
    total_profit = int(sum(pnl_krws))
    avg_return = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0

    return PerformanceSummary(
        total_trades=len(rows),
        win_trades=len(wins),
        loss_trades=len(losses),
        win_rate=len(wins) / len(pnl_pcts) if pnl_pcts else 0.0,
        avg_return_pct=round(avg_return, 2),
        total_profit=total_profit,
    )
