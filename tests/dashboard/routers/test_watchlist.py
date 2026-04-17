"""Watchlist 라우터 happy path — Redis + position_sheets."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def test_current_no_data(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/watchlist/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "no_data"


async def test_current_cached(app, redis_client):
    await redis_client.set(
        "watchlist:active",
        json.dumps({"stocks": [{"ticker": "005930", "score": 0.9}]}),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/watchlist/current")
        assert resp.status_code == 200
        assert resp.json()["stocks"][0]["ticker"] == "005930"


async def test_history(app, session_factory):
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO position_sheets "
                "(sheet_id, schema_version, generated_at, valid_until, ticker, strategy_tag, sheet_json, provenance_json) "
                "VALUES ('ps_w1', '1.1', :g, :v, '035720', 'breakout', '{}', '{}')"
            ),
            {"g": now, "v": now + timedelta(hours=4)},
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/watchlist/history")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["ticker"] == "035720"
