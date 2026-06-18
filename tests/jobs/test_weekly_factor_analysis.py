"""`weekly_factor_analysis` 스모크.

daily_quant_scores + daily_prices 를 fake conn 으로 주입, IC 산출 + 캐시 +
factor_ic_history 영속 확인. 2026-06-18 개편(유니버스 전체 + 다구간 + scope)
반영.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import fakeredis.aioredis
import pytest

from prime_jennie_runtime.jobs.factor_analysis import weekly_factor_analysis


class _FakeConn:
    def __init__(self, scores: list[dict], prices: list[dict]) -> None:
        self.scores = scores
        self.prices = prices
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM daily_quant_scores" in sql:
            return self.scores
        if "FROM daily_prices" in sql:
            return self.prices
        return []

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _score(
    code: str,
    score_date: date,
    *,
    momentum: float = 50.0,
    news: float = 50.0,
    supply: float = 50.0,
    sector_momentum: float = 5.0,
    selected: bool = False,
) -> dict:
    return {
        "score_date": score_date,
        "stock_code": code,
        "total_quant_score": momentum,
        "momentum_score": momentum,
        "quality_score": 50.0,
        "value_score": 50.0,
        "technical_score": 50.0,
        "news_score": news,
        "supply_demand_score": supply,
        "sector_momentum_score": sector_momentum,
        "is_final_selected": selected,
    }


def _price_series(code: str, start: date, closes: list[int]) -> list[dict]:
    return [
        {"stock_code": code, "price_date": start + timedelta(days=i), "close_price": v}
        for i, v in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_weekly_factor_analysis_no_scores_short_circuits():
    pool = _FakePool(_FakeConn(scores=[], prices=[]))
    redis_client = fakeredis.aioredis.FakeRedis()

    result = await weekly_factor_analysis(pool, redis_client)
    assert result == {"sample_count": 0, "skipped": "no_scores"}


@pytest.mark.asyncio
async def test_weekly_factor_analysis_insufficient_samples():
    today = date.today()
    scores = [_score(f"{i:06d}", today - timedelta(days=10)) for i in range(5)]
    prices: list[dict] = []
    for i in range(5):
        prices.extend(
            _price_series(
                f"{i:06d}",
                today - timedelta(days=10),
                [1000, 1010, 1020, 1030, 1040, 1050, 1060],
            )
        )
    pool = _FakePool(_FakeConn(scores=scores, prices=prices))
    redis_client = fakeredis.aioredis.FakeRedis()

    result = await weekly_factor_analysis(pool, redis_client)
    assert result["skipped"] == "insufficient_samples"
    assert result["sample_count"] == 5


@pytest.mark.asyncio
async def test_weekly_factor_analysis_computes_ic_and_caches():
    # 12종목: 점수 높을수록 forward return 높게 → 양의 IC 기대.
    today = date.today()
    score_date = today - timedelta(days=10)
    scores: list[dict] = []
    prices: list[dict] = []
    for i in range(12):
        code = f"{i:06d}"
        m = 5 + i * 8  # 5..93
        scores.append(_score(code, score_date, momentum=m, news=m, supply=m))
        end_price = int(1000 * (1 + i * 0.01))  # 0%..11%
        for offset, close in enumerate([1000, 1000, 1000, 1000, 1000, end_price]):
            prices.append(
                {
                    "stock_code": code,
                    "price_date": score_date + timedelta(days=offset),
                    "close_price": close,
                }
            )

    pool = _FakePool(_FakeConn(scores=scores, prices=prices))
    redis_client = fakeredis.aioredis.FakeRedis()

    result = await weekly_factor_analysis(pool, redis_client)
    assert result["sample_count"] == 12
    # 최상위 ic_spearman = universe T+5. momentum/news 단조 증가 → spearman 1.0
    assert result["ic_spearman"]["momentum_score"] == 1.0
    assert result["ic_spearman"]["news_score"] == 1.0
    # 조건부 성과: news_score >= 70 (i >= 9 → 3개)
    assert result["conditional_performance"]["high_news_score"]["count"] == 3
    assert result["conditional_performance"]["news_and_supply_combined"]["count"] == 3

    raw = await redis_client.get("factor:analysis:latest")
    saved = json.loads(raw)
    assert saved["sample_count"] == 12
    assert saved["by_scope"]["universe"]["t5"]["ic_spearman"]["momentum_score"] == 1.0


@pytest.mark.asyncio
async def test_universe_scope_includes_unselected_and_persists_ic():
    # 선정종목(3) 외 비선정종목까지 universe 로 본다 + factor_ic_history 영속.
    today = date.today()
    score_date = today - timedelta(days=12)
    scores: list[dict] = []
    prices: list[dict] = []
    for i in range(14):
        code = f"{i:06d}"
        m = 5 + i * 6
        scores.append(_score(code, score_date, momentum=m, news=m, supply=m, selected=i >= 11))
        end_price = int(1000 * (1 + i * 0.01))
        for offset, close in enumerate([1000, 1000, 1000, 1000, 1000, end_price]):
            prices.append(
                {
                    "stock_code": code,
                    "price_date": score_date + timedelta(days=offset),
                    "close_price": close,
                }
            )

    conn = _FakeConn(scores=scores, prices=prices)
    pool = _FakePool(conn)
    redis_client = fakeredis.aioredis.FakeRedis()

    result = await weekly_factor_analysis(pool, redis_client)
    # universe = 14 전부, selected = 3 (점수 분산 위해 universe 로 봐야 IC 의미 있음)
    assert result["by_scope"]["universe"]["t5"]["sample_count"] == 14
    assert result["by_scope"]["selected"]["t5"]["sample_count"] == 3
    # factor_ic_history 에 INSERT 발생
    assert any("factor_ic_history" in sql for sql, _ in conn.executed)
