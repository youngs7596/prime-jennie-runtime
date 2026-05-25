"""LLM stats 라우터 happy path — Redis hash 기반."""

from __future__ import annotations

from datetime import date

from httpx import ASGITransport, AsyncClient

from prime_jennie_runtime.dashboard.llm_pricing import (
    PRICING,
    VLLM_AMORTIZED_DAILY_USD,
    calc_shadow_cost,
)


async def test_features(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/features")
        assert resp.status_code == 200
        feats = resp.json()
        services = {f["service"] for f in feats}
        assert "macro" in services
        # scout 는 2026-05-22 결정론 quant 코어로 전환 — LLM feature 아님
        assert "scout" not in services
        assert all("model" in f for f in feats)


async def test_daily_stats(app, redis_client):
    today = date.today().isoformat()
    await redis_client.hset(
        f"llm:stats:{today}:macro",
        mapping={"calls": 42, "tokens_in": 1000, "tokens_out": 200},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["services"]["macro"]["calls"] == 42
        assert body["total"]["calls"] == 42


def test_calc_shadow_cost_uses_pricing_table():
    """1M token 단가가 가격표 그대로 환산되는지."""
    d = PRICING["deepseek"]["chat"]
    # tok_in = tok_out = 1M → in_per_million + out_per_million 정확히
    cost = calc_shadow_cost(1_000_000, 1_000_000)
    assert cost == d["in_per_million_usd"] + d["out_per_million_usd"]
    # 누락 provider/model 은 0
    assert calc_shadow_cost(1000, 1000, provider="unknown") == 0.0


async def test_daily_stats_includes_actual_and_shadow_cost(app, redis_client):
    """services / total 에 cost_actual_usd + cost_shadow_usd 가 채워진다."""
    today = date.today().isoformat()
    await redis_client.hset(
        f"llm:stats:{today}:macro",
        mapping={
            "calls": 7,
            "tokens_in": 12000,
            "tokens_out": 3500,
            "cost_usd": 0.012,
        },
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/stats")
        body = resp.json()
        macro = body["services"]["macro"]
        assert macro["cost_actual_usd"] == 0.012
        # shadow = deepseek 가격표 × token
        assert macro["cost_shadow_usd"] == calc_shadow_cost(12000, 3500)
        assert body["total"]["cost_actual_usd"] == 0.012
        assert body["total"]["cost_shadow_usd"] == macro["cost_shadow_usd"]


async def test_daily_stats_vllm_amortized_only_when_news_active(app, redis_client):
    """news_analysis 호출이 한 번이라도 있어야 vllm_amortized_daily_usd 가 부과."""
    today = date.today().isoformat()
    # 1) macro 만 — amortized 0
    await redis_client.hset(
        f"llm:stats:{today}:macro",
        mapping={"calls": 7, "tokens_in": 12000, "tokens_out": 3500},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/stats")
        assert resp.json()["vllm_amortized_daily_usd"] == 0.0

        # 2) news_analysis 1 호출 들어오면 amortized 가 daily 단가
        await redis_client.hset(
            f"llm:stats:{today}:news_analysis",
            mapping={"calls": 1, "tokens_in": 1000, "tokens_out": 200},
        )
        resp = await client.get("/api/llm/stats")
        assert resp.json()["vllm_amortized_daily_usd"] == VLLM_AMORTIZED_DAILY_USD


async def test_monthly_stats_amortizes_by_news_active_days(app, redis_client):
    """월별 vllm_amortized_monthly_usd = active days × daily 단가."""
    await redis_client.hset(
        "llm:stats:2026-05-23:news_analysis",
        mapping={"calls": 5, "tokens_in": 1000, "tokens_out": 200},
    )
    await redis_client.hset(
        "llm:stats:2026-05-24:news_analysis",
        mapping={"calls": 3, "tokens_in": 500, "tokens_out": 100},
    )
    # 5-25 는 macro 만, news 없음 → vllm active day 아님
    await redis_client.hset(
        "llm:stats:2026-05-25:macro",
        mapping={"calls": 2, "tokens_in": 200, "tokens_out": 50},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/stats/monthly/2026-05")
        body = resp.json()
        assert body["vllm_active_days"] == 2
        assert body["vllm_amortized_monthly_usd"] == 2 * VLLM_AMORTIZED_DAILY_USD
        assert body["days_in_month"] == 31
