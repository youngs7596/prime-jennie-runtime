"""Macro 라우터 happy path — macro_runs 조회."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO macro_runs "
                "(macro_run_id, generated_at, trigger_reason, gate, size_multiplier, reasoning, "
                "confidence, model_used, cost_usd, latency_ms, top_risks_json, metadata_json) "
                "VALUES ('macro_20260417', :g, 'scheduled_0800', 'open', 0.75, '정치 리스크 해소', "
                "'medium', 'claude-opus-4-7', 0.12, 1842, '[]', '{}')"
            ),
            {"g": now},
        )
        await session.commit()


async def test_regime_and_insight(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        regime = await client.get("/api/macro/regime")
        assert regime.status_code == 200, regime.text
        r = regime.json()
        assert r["gate"] == "open"
        assert r["size_multiplier"] == 0.75

        insight = await client.get("/api/macro/insight")
        assert insight.status_code == 200
        body = insight.json()
        assert body["macro_run_id"] == "macro_20260417"
        assert body["reasoning"] == "정치 리스크 해소"

        runs = await client.get("/api/macro/runs?limit=5")
        assert runs.status_code == 200
        assert len(runs.json()) == 1


async def test_regime_empty(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/macro/regime")
        assert resp.status_code == 200
        assert resp.json()["gate"] == "closed"
