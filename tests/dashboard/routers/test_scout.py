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


async def _seed_candidates(session_factory) -> None:
    """3건: promoted / rejected / pending 커버."""
    stmt = text(
        "INSERT INTO screening_candidates "
        "(scout_run_id, rank, ticker, strategy_tag, conviction, "
        "entry_hint_json, exit_hint_json, factors_json, notes, "
        "promoted_to_sheet_id, rejection_reason) VALUES "
        "(:run, :rank, :ticker, :tag, :conv, :eh, :xh, :fx, :notes, :sid, :rej)"
    )
    async with session_factory() as session:
        await session.execute(
            stmt,
            [
                {
                    "run": "scout_20260418_0830",
                    "rank": 0,
                    "ticker": "005930",
                    "tag": "momentum_breakout",
                    "conv": 0.82,
                    "eh": '{"trigger":"break_20d_high"}',
                    "xh": '{"stop":"-5%"}',
                    "fx": '{"rs_rank":92}',
                    "notes": "top pick",
                    "sid": "sheet_abc123",
                    "rej": None,
                },
                {
                    "run": "scout_20260418_0830",
                    "rank": 1,
                    "ticker": "000660",
                    "tag": "mean_reversion",
                    "conv": 0.55,
                    "eh": None,
                    "xh": None,
                    "fx": '{"zscore":-2.1}',
                    "notes": None,
                    "sid": None,
                    "rej": "macro_closed",
                },
                {
                    "run": "scout_20260418_0830",
                    "rank": 2,
                    "ticker": "035420",
                    "tag": "earnings_drift",
                    "conv": 0.71,
                    "eh": None,
                    "xh": None,
                    "fx": "{}",
                    "notes": None,
                    "sid": None,
                    "rej": None,
                },
            ],
        )
        await session.commit()


async def test_candidates_found(app, session_factory):
    await _seed(session_factory)
    await _seed_candidates(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/scout/runs/scout_20260418_0830/candidates")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 3
        # rank 순
        assert [r["rank"] for r in rows] == [0, 1, 2]
        # promoted
        assert rows[0]["ticker"] == "005930"
        assert rows[0]["promoted_to_sheet_id"] == "sheet_abc123"
        assert rows[0]["rejection_reason"] is None
        assert rows[0]["entry_hint"] == {"trigger": "break_20d_high"}
        assert rows[0]["conviction"] == 0.82
        # rejected
        assert rows[1]["promoted_to_sheet_id"] is None
        assert rows[1]["rejection_reason"] == "macro_closed"
        # pending (둘 다 NULL)
        assert rows[2]["promoted_to_sheet_id"] is None
        assert rows[2]["rejection_reason"] is None


async def test_candidates_empty_for_existing_run(app, session_factory):
    """run 은 있지만 후보 0건 — 200 + [] 반환 (404 아님)."""
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/scout/runs/scout_20260418_0830/candidates")
        assert resp.status_code == 200
        assert resp.json() == []


async def test_candidates_unknown_run(app):
    """없는 run 도 200 + [] — 엔드포인트는 screening_candidates 만 본다."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/scout/runs/scout_nonexistent/candidates")
        assert resp.status_code == 200
        assert resp.json() == []
