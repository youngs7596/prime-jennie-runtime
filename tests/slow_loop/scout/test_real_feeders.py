"""Real Scout feeder unit tests — _FakeEngine 로 SQL 파라미터/반환값 검증.

실 Postgres 라운드트립은 tests/slow_loop/test_pipeline_e2e_real.py 가 담당.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.scout.feeders.real import (
    RealMarketSummaryFeeder,
    RealNewsScoreFeeder,
    RealSectorMomentumFeeder,
    RealUniverseFeeder,
)
from prime_jennie_runtime.slow_loop.scout.schemas import MarketSummary, NewsScoreEntry

# --- fake engine scaffolding -----------------------------------------


@dataclass
class _FakeResult:
    rows: list[Any] = field(default_factory=list)

    def all(self) -> list[Any]:
        return list(self.rows)

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> Any:
        return self.rows[0] if self.rows else 0

    def scalar_one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


@dataclass
class _FakeConn:
    """execute() 응답을 sql substring → rows 매핑으로 반환."""

    responses: list[tuple[str, list[Any]]] = field(default_factory=list)
    executed: list[tuple[str, dict]] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, dict(params or {})))
        for needle, rows in self.responses:
            if needle in sql:
                return _FakeResult(rows=list(rows))
        return _FakeResult(rows=[])

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def begin(self) -> _FakeConn:
        return self.conn


# --- RealUniverseFeeder ---------------------------------------------


@pytest.mark.asyncio
async def test_universe_feeder_returns_top_n_codes_ordered():
    conn = _FakeConn(
        responses=[
            ("FROM stock_masters", [("005930",), ("000660",), ("035420",)]),
        ]
    )
    engine = _FakeEngine(conn)
    feeder = RealUniverseFeeder(engine=engine, size=3)  # type: ignore[arg-type]

    result = await feeder.fetch(date(2026, 4, 20))

    assert result == ["005930", "000660", "035420"]
    sql, params = conn.executed[0]
    assert "FROM stock_masters" in sql
    assert "is_active = TRUE" in sql
    assert "market_cap IS NOT NULL" in sql
    assert "ORDER BY market_cap DESC" in sql
    assert params == {"n": 3}


@pytest.mark.asyncio
async def test_universe_feeder_default_size_from_env(monkeypatch):
    monkeypatch.delenv("SCOUT_UNIVERSE_SIZE", raising=False)
    conn = _FakeConn(responses=[("FROM stock_masters", [])])
    feeder = RealUniverseFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]
    await feeder.fetch(date(2026, 4, 20))
    _, params = conn.executed[0]
    assert params == {"n": 200}


@pytest.mark.asyncio
async def test_universe_feeder_env_override(monkeypatch):
    monkeypatch.setenv("SCOUT_UNIVERSE_SIZE", "50")
    conn = _FakeConn(responses=[("FROM stock_masters", [])])
    feeder = RealUniverseFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]
    await feeder.fetch(date(2026, 4, 20))
    _, params = conn.executed[0]
    assert params == {"n": 50}


# --- RealNewsScoreFeeder --------------------------------------------


@pytest.mark.asyncio
async def test_news_score_feeder_returns_entries_per_ticker():
    latest = datetime(2026, 4, 20, 20, 0, 0)
    conn = _FakeConn(
        responses=[
            (
                "high_impact_count",  # aggregate 쿼리 (unique substring)
                [
                    {
                        "ticker": "005930",
                        "avg_score": 0.42,
                        "cnt": 5,
                        "high_impact_count": 2,
                        "latest": latest,
                    }
                ],
            ),
            (
                "GROUP BY ticker, event_type",
                [
                    {"ticker": "005930", "event_type": "earnings", "cnt": 3},
                    {"ticker": "005930", "event_type": "product", "cnt": 2},
                ],
            ),
        ]
    )
    feeder = RealNewsScoreFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]

    result = await feeder.fetch(date(2026, 4, 20), ["005930", "000660"])

    assert set(result.keys()) == {"005930", "000660"}
    e1 = result["005930"]
    assert isinstance(e1, NewsScoreEntry)
    assert e1.score == pytest.approx(0.42)
    assert e1.article_count == 5
    assert e1.timestamp == latest
    assert e1.high_impact_count == 2
    assert e1.event_types == {"earnings": 3, "product": 2}

    # 미매칭 ticker 는 zero entry.
    zero = result["000660"]
    assert zero.score == 0.0
    assert zero.article_count == 0
    assert zero.staleness_hours == pytest.approx(48.0)
    assert zero.high_impact_count == 0
    assert zero.event_types == {}


@pytest.mark.asyncio
async def test_news_score_feeder_passes_lookback_window():
    conn = _FakeConn(
        responses=[
            ("high_impact_count", []),
            ("GROUP BY ticker, event_type", []),
        ]
    )
    feeder = RealNewsScoreFeeder(engine=_FakeEngine(conn), lookback_hours=24.0)  # type: ignore[arg-type]
    await feeder.fetch(date(2026, 4, 20), ["005930"])
    sql, params = conn.executed[0]
    assert "analyzed_at >= :since" in sql
    assert "ticker = ANY(:tickers)" in sql
    assert params["tickers"] == ["005930"]
    # as_of 23:59:59 - 24h = 2026-04-19 23:59:59.
    assert params["since"] == datetime(2026, 4, 19, 23, 59, 59)
    # 2 쿼리 (집계 + event_type 분포)
    assert len(conn.executed) == 2


@pytest.mark.asyncio
async def test_news_score_feeder_empty_universe_short_circuits():
    conn = _FakeConn()
    feeder = RealNewsScoreFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]
    result = await feeder.fetch(date(2026, 4, 20), [])
    assert result == {}
    assert conn.executed == []


@pytest.mark.asyncio
async def test_news_score_feeder_clamps_out_of_range_score():
    latest = datetime(2026, 4, 20, 10, 0, 0)
    conn = _FakeConn(
        responses=[
            (
                "high_impact_count",
                [
                    {
                        "ticker": "005930",
                        "avg_score": 2.5,
                        "cnt": 1,
                        "high_impact_count": 0,
                        "latest": latest,
                    }
                ],
            ),
            ("GROUP BY ticker, event_type", []),
        ]
    )
    feeder = RealNewsScoreFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]
    result = await feeder.fetch(date(2026, 4, 20), ["005930"])
    assert result["005930"].score == 1.0


# --- RealSectorMomentumFeeder ---------------------------------------


@pytest.mark.asyncio
async def test_sector_momentum_empty_daily_prices_returns_empty(caplog):
    conn = _FakeConn(responses=[("SELECT COUNT(*) FROM daily_prices", [0])])
    feeder = RealSectorMomentumFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]

    import logging

    with caplog.at_level(logging.WARNING):
        result = await feeder.fetch(date(2026, 4, 20))

    assert result == {}
    assert any("daily_prices 가 비어있음" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_sector_momentum_returns_per_sector_avg():
    end = date(2026, 4, 20)
    start = date(2026, 3, 20)
    conn = _FakeConn(
        responses=[
            ("SELECT COUNT(*) FROM daily_prices", [100]),
            ("SELECT MAX(price_date) FROM daily_prices WHERE price_date <= :t", [end]),
            ("SELECT MIN(price_date) FROM daily_prices", [start]),
            (
                "GROUP BY sm.sector_group",
                [
                    {"sector": "반도체", "momentum": 0.08},
                    {"sector": "바이오", "momentum": -0.02},
                    {"sector": "건설", "momentum": None},
                ],
            ),
        ]
    )
    feeder = RealSectorMomentumFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]
    result = await feeder.fetch(date(2026, 4, 20))
    assert result == {"반도체": pytest.approx(0.08), "바이오": pytest.approx(-0.02)}


@pytest.mark.asyncio
async def test_sector_momentum_returns_empty_if_start_equals_end():
    same = date(2026, 4, 20)
    conn = _FakeConn(
        responses=[
            ("SELECT COUNT(*) FROM daily_prices", [10]),
            ("SELECT MAX(price_date) FROM daily_prices WHERE price_date <= :t", [same]),
            ("SELECT MIN(price_date) FROM daily_prices", [same]),
        ]
    )
    feeder = RealSectorMomentumFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]
    result = await feeder.fetch(date(2026, 4, 20))
    assert result == {}


# --- RealMarketSummaryFeeder ----------------------------------------


@pytest.mark.asyncio
async def test_market_summary_combines_index_and_breadth():
    conn = _FakeConn(
        responses=[
            # KOSPI index latest
            (
                "WHERE index_code = :c ORDER BY price_date DESC LIMIT 1",
                [{"close_price": 2800.0, "change_pct": 1.23}],  # first call matches
            ),
            ("SELECT COUNT(*) FROM daily_prices", [100]),
            (
                "SELECT MAX(price_date) FROM daily_prices WHERE price_date <= :t",
                [date(2026, 4, 20)],
            ),
            (
                "SUM(CASE WHEN change_pct > 0",
                [{"up_count": 520, "down_count": 310}],
            ),
        ]
    )
    feeder = RealMarketSummaryFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]

    result = await feeder.fetch(date(2026, 4, 20))

    assert isinstance(result, MarketSummary)
    assert result.kospi_close == 2800.0
    # DB percent 1.23 → schema 소수 0.0123.
    assert result.kospi_change_pct == pytest.approx(0.0123)
    # 두 번째 index 호출도 같은 needle 에 매칭되므로 KOSDAQ 도 같은 값.
    assert result.kosdaq_close == 2800.0
    assert result.up_count == 520
    assert result.down_count == 310


@pytest.mark.asyncio
async def test_market_summary_index_missing_returns_zero():
    conn = _FakeConn(
        responses=[
            ("SELECT COUNT(*) FROM daily_prices", [0]),
        ]
    )
    feeder = RealMarketSummaryFeeder(engine=_FakeEngine(conn))  # type: ignore[arg-type]

    result = await feeder.fetch(date(2026, 4, 20))

    assert result.kospi_close == 0.0
    assert result.kosdaq_close == 0.0
    # daily_prices empty → up/down=0 placeholder.
    assert result.up_count == 0
    assert result.down_count == 0
