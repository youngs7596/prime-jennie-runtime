"""Scout 라우터 happy path — 결정론 quant scout_runs 조회."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    """결정론 scout run 1건 — code_hash/code_text 는 스코어러 버전 마커, LLM 메타 없음."""
    now = datetime.now(UTC)
    meta = {
        "factor_weights": {"momentum": 0.25, "quality": 0.20, "value": 0.15},
        "strategy_tags_used": ["SECTOR_MOMENTUM", "MEAN_REVERT_RSI"],
        "fallback_strategy": "skip_today",
        "estimated_runtime_seconds": 4.2,
    }
    ctx = {
        "universe_size": 300,
        "macro_gate": "open",
        "macro_size_multiplier": 1.0,
        "scorer_version": "deterministic-quant-v2-port@1",
    }
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO scout_runs "
                "(scout_run_id, generated_at, code_hash, code_text, hypothesis, "
                "candidates_count, prompt_version, metadata_json, context_snapshot_json) "
                "VALUES (:id, :g, :hash, :code, :hyp, :cnt, :ver, :meta, :ctx)"
            ),
            {
                "id": "scout_20260418_0830",
                "g": now,
                "hash": "stable_marker_hash",
                "code": "# deterministic-quant-v2-port@1\n",
                "hyp": "결정론 quant v2 포팅: universe 300 → 채점 280 → 선정 12",
                "cnt": 12,
                "ver": "deterministic-quant-v2-port@1",
                "meta": json.dumps(meta),
                "ctx": json.dumps(ctx),
            },
        )
        await session.commit()


async def test_latest_and_runs_and_detail(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        latest = await client.get("/api/scout/latest")
        assert latest.status_code == 200, latest.text
        body = latest.json()
        assert body["scout_run_id"] == "scout_20260418_0830"
        assert body["summary"].startswith("결정론 quant")
        assert body["candidates_count"] == 12
        assert body["scorer_version"] == "deterministic-quant-v2-port@1"
        assert body["strategy_tags"] == ["SECTOR_MOMENTUM", "MEAN_REVERT_RSI"]
        assert body["runtime_seconds"] == 4.2
        # LLM codegen 개념 제거 — 요약에 노출되지 않아야 함
        assert "code_text" not in body
        assert "model_used" not in body
        assert "factor_weights" not in body  # 상세 전용

        runs = await client.get("/api/scout/runs?limit=5")
        assert runs.status_code == 200
        data = runs.json()
        assert len(data) == 1
        assert data[0]["scorer_version"] == "deterministic-quant-v2-port@1"
        assert "code_text" not in data[0]

        detail = await client.get("/api/scout/runs/scout_20260418_0830")
        assert detail.status_code == 200
        det = detail.json()
        assert det["summary"].startswith("결정론 quant")
        assert det["factor_weights"]["momentum"] == 0.25
        assert det["context"]["macro_gate"] == "open"
        assert "code_text" not in det


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


async def _seed_stock_masters(session_factory) -> None:
    """candidates JOIN 검증용 — 일부는 의도적으로 누락 (stock_name NULL 케이스)."""
    stmt = text("INSERT INTO stock_masters (stock_code, stock_name) VALUES (:c, :n)")
    async with session_factory() as session:
        await session.execute(
            stmt,
            [
                {"c": "005930", "n": "삼성전자"},
                {"c": "000660", "n": "SK하이닉스"},
                # 035420 (NAVER) 는 의도적 누락 — stock_name None 확인
            ],
        )
        await session.commit()


async def test_candidates_found(app, session_factory):
    await _seed(session_factory)
    await _seed_candidates(session_factory)
    await _seed_stock_masters(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/scout/runs/scout_20260418_0830/candidates")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 3
        # rank 순
        assert [r["rank"] for r in rows] == [0, 1, 2]
        # promoted
        assert rows[0]["ticker"] == "005930"
        assert rows[0]["stock_name"] == "삼성전자"
        assert rows[0]["promoted_to_sheet_id"] == "sheet_abc123"
        assert rows[0]["rejection_reason"] is None
        assert rows[0]["entry_hint"] == {"trigger": "break_20d_high"}
        assert rows[0]["conviction"] == 0.82
        # rejected
        assert rows[1]["stock_name"] == "SK하이닉스"
        assert rows[1]["promoted_to_sheet_id"] is None
        assert rows[1]["rejection_reason"] == "macro_closed"
        # pending + stock_masters 에 누락 → stock_name None
        assert rows[2]["ticker"] == "035420"
        assert rows[2]["stock_name"] is None
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
