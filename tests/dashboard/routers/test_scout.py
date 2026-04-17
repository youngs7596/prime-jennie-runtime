"""Scout 라우터 happy path — scout_runs 조회."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO scout_runs "
                "(scout_run_id, generated_at, code_hash, code_text, hypothesis, "
                "candidates_count, model_used, prompt_version, cost_usd, metadata_json) "
                "VALUES ('scout_20260418_0830', :g, 'abc123', 'def screen(df): return df.head(5)', "
                "'장세 조정 이후 저점 매수', 5, 'deepseek-chat', 'v1', 0.002, '{}')"
            ),
            {"g": now},
        )
        await session.commit()


async def test_latest_and_runs_and_detail(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        latest = await client.get("/api/scout/latest")
        assert latest.status_code == 200, latest.text
        body = latest.json()
        assert body["scout_run_id"] == "scout_20260418_0830"
        assert body["hypothesis"] == "장세 조정 이후 저점 매수"
        assert body["candidates_count"] == 5
        assert "code_text" not in body  # summary 에는 code 제외

        runs = await client.get("/api/scout/runs?limit=5")
        assert runs.status_code == 200
        data = runs.json()
        assert len(data) == 1
        assert data[0]["model_used"] == "deepseek-chat"
        assert "code_text" not in data[0]

        detail = await client.get("/api/scout/runs/scout_20260418_0830")
        assert detail.status_code == 200
        det = detail.json()
        assert det["code_text"].startswith("def screen")
        assert det["cost_usd"] == 0.002


async def test_latest_empty(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/scout/latest")
        assert resp.status_code == 200
        assert resp.json() == {"status": "no_data"}


async def test_run_not_found(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/scout/runs/scout_nonexistent")
        assert resp.status_code == 404
