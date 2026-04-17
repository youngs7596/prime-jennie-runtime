"""`analyze_ai_performance` + `analyst_feedback` 스모크.

legacy_trade_logs query 는 fake conn, Redis 는 fakeredis.aioredis.
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from prime_jennie_runtime.jobs.analytics import (
    analyst_feedback,
    analyze_ai_performance,
)


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetch_calls.append((sql, args))
        return self.rows


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _FakeConn(rows)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_analyze_ai_performance_aggregates_and_writes_redis():
    rows = [
        {"reason": "TARGET", "market_regime": "BULL", "hybrid_score": 85, "profit_pct": 5.0},
        {"reason": "TARGET", "market_regime": "BULL", "hybrid_score": 82, "profit_pct": 3.0},
        {"reason": "STOP_LOSS", "market_regime": "BEAR", "hybrid_score": 45, "profit_pct": -4.0},
        {"reason": "TIMEOUT", "market_regime": "BULL", "hybrid_score": None, "profit_pct": -1.0},
    ]
    pool = _FakePool(rows)
    redis_client = fakeredis.aioredis.FakeRedis()

    result = await analyze_ai_performance(pool, redis_client, period_days=30)

    assert result["total_trades"] == 4
    # 2 wins (5.0, 3.0) out of 4 = 50.0%
    assert result["win_rate"] == 50.0
    assert result["avg_profit_pct"] == 0.75  # (5+3-4-1)/4

    assert result["by_reason"]["TARGET"]["count"] == 2
    assert result["by_reason"]["TARGET"]["win_rate"] == 100.0
    assert result["by_regime"]["BEAR"]["wins"] == 0
    assert result["by_score_bin"]["80+"]["count"] == 2
    assert result["by_score_bin"]["no_score"]["count"] == 1
    assert result["by_score_bin"]["40-59"]["count"] == 1

    raw = await redis_client.get("ai:performance:latest")
    saved = json.loads(raw)
    assert saved["total_trades"] == 4


@pytest.mark.asyncio
async def test_analyze_ai_performance_handles_no_rows():
    pool = _FakePool([])
    redis_client = fakeredis.aioredis.FakeRedis()

    result = await analyze_ai_performance(pool, redis_client, period_days=30)
    assert result["total_trades"] == 0
    assert result["win_rate"] == 0
    assert result["by_reason"] == {}

    raw = await redis_client.get("ai:performance:latest")
    assert raw is not None  # 빈 결과도 캐시한다 (downstream feedback 가 명시적 RuntimeError 받도록)


@pytest.mark.asyncio
async def test_analyst_feedback_renders_markdown_from_perf_cache():
    redis_client = fakeredis.aioredis.FakeRedis()
    perf = {
        "period_days": 30,
        "total_trades": 10,
        "win_rate": 70.0,
        "avg_profit_pct": 4.5,
        "by_reason": {"TARGET": {"count": 7, "win_rate": 100.0, "avg_pct": 5.0}},
        "by_regime": {"BULL": {"count": 8, "win_rate": 75.0, "avg_pct": 4.0}},
        "by_score_bin": {"80+": {"count": 5, "win_rate": 100.0, "avg_pct": 6.0}},
    }
    await redis_client.set("ai:performance:latest", json.dumps(perf, ensure_ascii=False))

    report = await analyst_feedback(redis_client)

    assert "# AI Trading Performance Report" in report
    assert "**총 거래**: 10건" in report
    assert "**승률**: 70.0%" in report
    assert "**TARGET**: 7건" in report
    assert "승률이 양호합니다" in report  # win_rate 70 → 양호 메시지

    saved = await redis_client.get("analyst:feedback:summary")
    assert saved is not None
    updated = await redis_client.get("analyst:feedback:updated_at")
    assert updated is not None


@pytest.mark.asyncio
async def test_analyst_feedback_low_winrate_emits_warning():
    redis_client = fakeredis.aioredis.FakeRedis()
    perf = {
        "period_days": 30,
        "total_trades": 5,
        "win_rate": 20.0,
        "avg_profit_pct": -2.0,
        "by_reason": {"STOP_LOSS": {"count": 4, "win_rate": 0, "avg_pct": -3.0}},
        "by_regime": {},
        "by_score_bin": {},
    }
    await redis_client.set("ai:performance:latest", json.dumps(perf, ensure_ascii=False))

    report = await analyst_feedback(redis_client)
    assert "승률이 낮습니다" in report
    assert "STOP_LOSS" in report  # worst_reason 라인


@pytest.mark.asyncio
async def test_analyst_feedback_raises_when_no_perf_cache():
    redis_client = fakeredis.aioredis.FakeRedis()
    with pytest.raises(RuntimeError, match="Run analyze_ai_performance"):
        await analyst_feedback(redis_client)
