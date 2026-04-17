"""LLM stats 라우터 happy path — Redis hash 기반."""

from __future__ import annotations

from datetime import date

from httpx import ASGITransport, AsyncClient


async def test_features(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/features")
        assert resp.status_code == 200
        feats = resp.json()
        assert any(f["service"] == "scout" for f in feats)
        assert all("model" in f for f in feats)


async def test_daily_stats(app, redis_client):
    today = date.today().isoformat()
    await redis_client.hset(
        f"llm:stats:{today}:scout",
        mapping={"calls": 42, "tokens_in": 1000, "tokens_out": 200},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/llm/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["services"]["scout"]["calls"] == 42
        assert body["total"]["calls"] == 42
