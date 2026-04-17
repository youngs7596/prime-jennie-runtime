"""Trades 라우터 happy path — executions + outcomes 조인."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO position_sheets "
                "(sheet_id, schema_version, generated_at, valid_until, ticker, strategy_tag, sheet_json, provenance_json) "
                "VALUES ('ps_1', '1.1', :g, :v, '005930', 'gap_up', '{}', '{}')"
            ),
            {"g": now - timedelta(hours=2), "v": now + timedelta(hours=4)},
        )
        await session.execute(
            text(
                "INSERT INTO executions (sheet_id, side, price, qty, executed_at, slippage_bps) "
                "VALUES ('ps_1', 'buy', 70000, 10, :at, 3.5)"
            ),
            {"at": now - timedelta(hours=1)},
        )
        await session.execute(
            text(
                "INSERT INTO executions (sheet_id, side, price, qty, executed_at) "
                "VALUES ('ps_1', 'sell', 72000, 10, :at)"
            ),
            {"at": now - timedelta(minutes=30)},
        )
        await session.execute(
            text(
                "INSERT INTO outcomes (sheet_id, entry_price, exit_price, pnl_krw, pnl_pct, exit_reason, closed_at) "
                "VALUES ('ps_1', 70000, 72000, 20000, 2.857, 'tp', :at)"
            ),
            {"at": now - timedelta(minutes=30)},
        )
        await session.commit()


async def test_recent_trades(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/trades/recent")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 2
        sell = next(r for r in rows if r["trade_type"] == "SELL")
        buy = next(r for r in rows if r["trade_type"] == "BUY")
        assert sell["ticker"] == "005930"
        assert sell["profit_pct"] is not None
        assert buy["profit_pct"] is None  # BUY 행은 pnl 없음
        assert sell["total_amount"] == sell["price"] * sell["quantity"]
