"""Paper outcomes API — paper alpha 측정 결과 + KODEX200 벤치마크 비교 (P4).

paper 모드 (2026-05-29 전환) 의 핵심 질문 "이 전략이 같은 기간 KODEX200 을 사서
들고 있는 것보다 나은가" 에 답하는 데이터를 control-ui 에 공급한다.

- 시트별 paper PnL 은 paper_outcomes (simulator 측정 결과)
- 벤치마크는 같은 보유 기간의 KODEX200 (069500) 수익률
- alpha = 시트 PnL - 같은 기간 벤치마크 PnL

데이터 없음 (벤치마크 일봉 결손, data_missing 측정) 은 None 으로 노출 — UI 가
구분 표시. 집계는 Python 에서 수행 (SQLite 테스트 호환 + 단순성).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session

router = APIRouter(prefix="/paper", tags=["paper"])

# 벤치마크 — KODEX200 ETF. daily_prices 에 일봉이 수집되어 있다.
BENCHMARK_TICKER = "069500"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PaperOutcomeRecord(BaseModel):
    sheet_id: str
    ticker: str | None = None
    strategy_tag: str | None = None
    entry_date: date
    exit_date: date | None = None
    holding_days: int | None = None
    pnl_pct: float | None = None
    exit_reason: str | None = None
    coverage: str | None = None
    simulator_version: str | None = None
    benchmark_pnl_pct: float | None = None
    alpha_pct: float | None = None


class GroupStats(BaseModel):
    count: int
    win_rate: float | None = None  # pnl > 0 비율 (측정 실패 제외)
    avg_pnl_pct: float | None = None
    avg_alpha_pct: float | None = None


class PaperSummary(BaseModel):
    total_sheets_measured: int
    data_missing_count: int
    overall: GroupStats
    benchmark_ticker: str = BENCHMARK_TICKER
    by_strategy: dict[str, GroupStats]
    by_exit_reason: dict[str, GroupStats]
    by_coverage: dict[str, GroupStats]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_benchmark_closes(session: AsyncSession) -> dict[date, float]:
    """벤치마크 (KODEX200) 일봉 close 전체를 날짜 dict 로."""
    result = await session.execute(
        text("SELECT price_date, close_price FROM daily_prices WHERE stock_code = :ticker"),
        {"ticker": BENCHMARK_TICKER},
    )
    closes: dict[date, float] = {}
    for row in result.mappings().all():
        d = row["price_date"]
        # SQLite 는 DATE 를 문자열로 돌려줄 수 있다.
        if isinstance(d, str):
            d = date.fromisoformat(d)
        closes[d] = float(row["close_price"])
    return closes


def _benchmark_pnl(
    closes: dict[date, float], entry: date | str | None, exit_: date | str | None
) -> float | None:
    """같은 보유 기간의 벤치마크 수익률 (%). 어느 한쪽 일봉이 없으면 None."""
    if entry is None or exit_ is None:
        return None
    if isinstance(entry, str):
        entry = date.fromisoformat(entry)
    if isinstance(exit_, str):
        exit_ = date.fromisoformat(exit_)
    b_in = closes.get(entry)
    b_out = closes.get(exit_)
    if b_in is None or b_out is None or b_in <= 0:
        return None
    return (b_out - b_in) / b_in * 100.0


def _group_stats(records: list[PaperOutcomeRecord]) -> GroupStats:
    """측정 성공 (pnl 존재) 레코드들의 집계."""
    measured = [r for r in records if r.pnl_pct is not None]
    if not measured:
        return GroupStats(count=len(records))
    wins = sum(1 for r in measured if (r.pnl_pct or 0) > 0)
    alphas = [r.alpha_pct for r in measured if r.alpha_pct is not None]
    return GroupStats(
        count=len(records),
        win_rate=round(wins / len(measured) * 100.0, 1),
        avg_pnl_pct=round(sum(r.pnl_pct or 0 for r in measured) / len(measured), 3),
        avg_alpha_pct=round(sum(alphas) / len(alphas), 3) if alphas else None,
    )


async def _load_outcome_records(
    session: AsyncSession, *, since: date | None = None
) -> list[PaperOutcomeRecord]:
    where = "WHERE 1=1"
    params: dict[str, Any] = {}
    if since is not None:
        where += " AND po.entry_date >= :since"
        params["since"] = since

    result = await session.execute(
        text(
            "SELECT po.sheet_id, po.entry_date, po.exit_date, po.holding_days, po.pnl_pct, "
            "po.exit_reason, po.coverage, po.simulator_version, ps.ticker, ps.strategy_tag "
            "FROM paper_outcomes po "
            "LEFT JOIN position_sheets ps ON ps.sheet_id = po.sheet_id "
            f"{where} "
            "ORDER BY po.entry_date DESC, po.sheet_id"
        ),
        params,
    )
    rows = result.mappings().all()
    closes = await _load_benchmark_closes(session)

    records: list[PaperOutcomeRecord] = []
    for row in rows:
        entry_d = row["entry_date"]
        exit_d = row["exit_date"]
        if isinstance(entry_d, str):
            entry_d = date.fromisoformat(entry_d)
        if isinstance(exit_d, str):
            exit_d = date.fromisoformat(exit_d)
        pnl = float(row["pnl_pct"]) if row["pnl_pct"] is not None else None
        bench = _benchmark_pnl(closes, entry_d, exit_d)
        records.append(
            PaperOutcomeRecord(
                sheet_id=row["sheet_id"],
                ticker=row["ticker"],
                strategy_tag=row["strategy_tag"],
                entry_date=entry_d,
                exit_date=exit_d,
                holding_days=row["holding_days"],
                pnl_pct=pnl,
                exit_reason=row["exit_reason"],
                coverage=row["coverage"],
                simulator_version=row["simulator_version"],
                benchmark_pnl_pct=round(bench, 3) if bench is not None else None,
                alpha_pct=round(pnl - bench, 3) if pnl is not None and bench is not None else None,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/outcomes", response_model=list[PaperOutcomeRecord])
async def get_paper_outcomes(
    days: int = 30,
    session: AsyncSession = Depends(get_session),
) -> list[PaperOutcomeRecord]:
    """최근 `days` 일 (entry_date 기준) 의 시트별 측정 결과 + 벤치마크/알파."""
    since = date.today() - timedelta(days=max(1, days))
    return await _load_outcome_records(session, since=since)


@router.get("/summary", response_model=PaperSummary)
async def get_paper_summary(
    session: AsyncSession = Depends(get_session),
) -> PaperSummary:
    """전체 측정 요약 — 승률/평균 PnL/알파를 전체·전략별·청산사유별·coverage 별로."""
    records = await _load_outcome_records(session)

    data_missing = [r for r in records if r.exit_reason == "data_missing"]
    valid = [r for r in records if r.exit_reason != "data_missing"]

    def _group_by(key_fn) -> dict[str, GroupStats]:
        groups: dict[str, list[PaperOutcomeRecord]] = {}
        for r in valid:
            key = key_fn(r) or "unknown"
            groups.setdefault(key, []).append(r)
        return {k: _group_stats(v) for k, v in sorted(groups.items())}

    return PaperSummary(
        total_sheets_measured=len(records),
        data_missing_count=len(data_missing),
        overall=_group_stats(valid),
        by_strategy=_group_by(lambda r: r.strategy_tag),
        by_exit_reason=_group_by(lambda r: r.exit_reason),
        by_coverage=_group_by(lambda r: r.coverage),
    )
