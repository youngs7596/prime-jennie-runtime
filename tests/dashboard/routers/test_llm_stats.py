"""LLM stats 라우터 happy path — Redis hash 기반."""

from __future__ import annotations

from datetime import date

from httpx import ASGITransport, AsyncClient


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
