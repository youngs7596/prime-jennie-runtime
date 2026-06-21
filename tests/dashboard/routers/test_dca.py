"""DCA 라우터 — 진행률 카드 데이터(캠페인 + 최근 슬라이스)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO stock_masters (stock_code, stock_name, market, sector_group) "
                "VALUES ('005930', '삼성전자', 'KOSPI', '반도체/IT'), "
                "('000660', 'SK하이닉스', 'KOSPI', '반도체/IT')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO dca_campaigns "
                "(id, ticker, status, preset, cap_krw, total_slices, execute_at_kst, "
                "cumulative_filled_krw, cumulative_filled_qty, slices_done, dry_run) VALUES "
                # 진행 중: 2슬라이스 체결, 누적 23,600,000 / 상한 118,000,000
                "('dca:005930:20260620', '005930', 'active', 'production', 118000000, 10, "
                "'14:00', 23600000, 200, 2, 0), "
                # 막 무장, 아직 미집행
                "('dca:000660:20260620', '000660', 'active', 'production', 118000000, 10, "
                "'14:00', 0, 0, 0, 0), "
                # 종료된 스모크
                "('dca:smoke:015760:20260620', '015760', 'completed', 'smoke', 50000, 1, "
                "'10:00', 37900, 1, 1, 0)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO dca_slice_executions "
                "(slice_key, campaign_id, ticker, slice_no, trigger_kind, trade_date, status, "
                "budget_krw, filled_qty, filled_krw, avg_price, reject_reason) VALUES "
                "('dca:005930:20260620:2026-06-22:sched', 'dca:005930:20260620', '005930', 1, "
                "'scheduled', '2026-06-22', 'filled', 11800000, 100, 11800000, 118000, NULL), "
                "('dca:005930:20260620:2026-06-23:sched', 'dca:005930:20260620', '005930', 2, "
                "'scheduled', '2026-06-23', 'filled', 11800000, 100, 11800000, 118000, NULL)"
            )
        )
        await session.commit()


async def test_campaigns_progress(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/dca/campaigns")
        assert resp.status_code == 200, resp.text
        body = resp.json()

    camps = body["campaigns"]
    assert len(camps) == 3
    # active 가 먼저 (id 순) → 000660, 005930, 그 뒤 completed smoke
    assert [c["status"] for c in camps] == ["active", "active", "completed"]

    samsung = next(c for c in camps if c["ticker"] == "005930")
    assert samsung["stock_name"] == "삼성전자"
    assert samsung["slices_done"] == 2
    assert samsung["progress_pct"] == 20.0  # 23.6M / 118M
    assert samsung["avg_price"] == 118000.0  # 23,600,000 / 200
    assert len(samsung["recent_slices"]) == 2
    # 최근 슬라이스가 날짜 내림차순(6-23 먼저)
    assert samsung["recent_slices"][0]["trade_date"] == "2026-06-23"
    assert samsung["recent_slices"][0]["slice_no"] == 2

    armed = next(c for c in camps if c["ticker"] == "000660")
    assert armed["avg_price"] is None  # 체결 0
    assert armed["progress_pct"] == 0.0
    assert armed["recent_slices"] == []


async def test_campaigns_empty(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/dca/campaigns")
        assert resp.status_code == 200, resp.text
        assert resp.json()["campaigns"] == []
