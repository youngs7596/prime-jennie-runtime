"""결정론 enrichment — 순수 헬퍼 + SQL 패턴 테스트.

DB batch 로더 검증 중 SQL 형태와 row→model 매핑은 _FakeEngine 로 가능,
실 Postgres 라운드트립은 통합 테스트에서 담당.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.scout.enrichment import (
    DailyPrice,
    _build_snapshot,
    _load_fundamentals,
    _sentiment_to_0_100,
)


def _price(close: int, high: int, low: int) -> DailyPrice:
    return DailyPrice(
        stock_code="005930",
        price_date=date(2026, 5, 1),
        open_price=close,
        high_price=high,
        low_price=low,
        close_price=close,
        volume=1_000_000,
    )


class TestSentimentTo0100:
    def test_neutral_maps_to_50(self):
        assert _sentiment_to_0_100(0.0) == 50.0

    def test_max_positive_maps_to_100(self):
        assert _sentiment_to_0_100(1.0) == 100.0

    def test_max_negative_maps_to_0(self):
        assert _sentiment_to_0_100(-1.0) == 0.0

    def test_observed_positive_avg(self):
        """실측 평균 0.38 → ~69."""
        assert _sentiment_to_0_100(0.38) == 69.0

    def test_clamped_above_1(self):
        assert _sentiment_to_0_100(2.0) == 100.0

    def test_clamped_below_minus1(self):
        assert _sentiment_to_0_100(-3.0) == 0.0


class TestBuildSnapshot:
    def test_none_for_empty_prices(self):
        assert _build_snapshot("005930", []) is None

    def test_price_is_latest_close(self):
        prices = [_price(100, 110, 90), _price(120, 130, 115)]
        snap = _build_snapshot("005930", prices)
        assert snap is not None
        assert snap.price == 120

    def test_high_52w_is_window_max(self):
        prices = [_price(100, 150, 90), _price(120, 130, 115)]
        snap = _build_snapshot("005930", prices)
        assert snap is not None
        assert snap.high_52w == 150  # 윈도우 내 최고 high

    def test_change_pct_from_prev_close(self):
        prices = [_price(100, 110, 90), _price(110, 120, 105)]
        snap = _build_snapshot("005930", prices)
        assert snap is not None
        assert snap.change_pct == 10.0  # 110/100 - 1


# --- _load_fundamentals SQL 패턴 -------------------------------------


@dataclass
class _FakeResult:
    rows: list[Any] = field(default_factory=list)

    def all(self) -> list[Any]:
        return list(self.rows)

    def mappings(self) -> _FakeResult:
        return self


@dataclass
class _FakeConn:
    rows: list[dict] = field(default_factory=list)
    executed: list[tuple[str, dict]] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _FakeResult:
        self.executed.append((str(stmt), dict(params or {})))
        return _FakeResult(rows=list(self.rows))

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def connect(self) -> _FakeConn:
        return self.conn


class TestLoadFundamentalsSql:
    """SQL 이 컬럼별 최신 non-null 를 잡는 CTE 패턴인지 확인.

    2026-05-26 회귀 방지: 단일 DISTINCT ON 으로 돌아가면 ROE-only 월간 갱신이
    PER/PBR null 행을 최신으로 골라 quality/value 점수를 왜곡한다.
    """

    @pytest.mark.asyncio
    async def test_sql_has_column_wise_latest_ctes(self):
        conn = _FakeConn(rows=[])
        engine = _FakeEngine(conn)

        await _load_fundamentals(engine, ["005930"], as_of=date(2026, 5, 26))  # type: ignore[arg-type]

        sql, params = conn.executed[0]
        assert "per_latest" in sql
        assert "pbr_latest" in sql
        assert "roe_latest" in sql
        assert "per IS NOT NULL" in sql
        assert "pbr IS NOT NULL" in sql
        assert "roe IS NOT NULL" in sql
        assert params == {"codes": ["005930"], "as_of": date(2026, 5, 26)}

    @pytest.mark.asyncio
    async def test_returns_financial_trend_from_rows(self):
        conn = _FakeConn(
            rows=[
                {"stock_code": "005930", "per": 11.04, "pbr": 3.73, "roe": 44.15},
                {"stock_code": "000660", "per": None, "pbr": None, "roe": 12.5},
            ]
        )
        engine = _FakeEngine(conn)

        out = await _load_fundamentals(engine, ["005930", "000660"], as_of=date(2026, 5, 26))  # type: ignore[arg-type]

        assert out["005930"].per == 11.04
        assert out["005930"].pbr == 3.73
        assert out["005930"].roe == 44.15
        assert out["000660"].per is None
        assert out["000660"].roe == 12.5
