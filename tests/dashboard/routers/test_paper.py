"""Paper 라우터 — 측정 결과 + KODEX200 벤치마크/알파."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    """시트 2 (측정 성공) + 1 (data_missing) + 벤치마크 일봉."""
    now = datetime.now(UTC)
    entry = date.today() - timedelta(days=10)
    exit_win = entry + timedelta(days=3)
    exit_lose = entry + timedelta(days=5)

    async with session_factory() as session:
        for sid, ticker, tag in [
            ("ps_p1", "005930", "GAP_UP_REBOUND"),
            ("ps_p2", "000660", "SECTOR_MOMENTUM"),
            ("ps_p3", "035720", "SECTOR_MOMENTUM"),
        ]:
            await session.execute(
                text(
                    "INSERT INTO position_sheets "
                    "(sheet_id, schema_version, generated_at, valid_until, ticker, "
                    "strategy_tag, sheet_json, provenance_json) "
                    "VALUES (:sid, '1.1', :g, :v, :ticker, :tag, '{}', '{}')"
                ),
                {
                    "sid": sid,
                    "g": now - timedelta(days=10),
                    "v": now - timedelta(days=10) + timedelta(hours=5),
                    "ticker": ticker,
                    "tag": tag,
                },
            )

        # 측정 성공 — 익절 +5% (3일 보유)
        await session.execute(
            text(
                "INSERT INTO paper_outcomes "
                "(sheet_id, simulator_version, entry_model, coverage, entry_date, entry_price, "
                "exit_date, exit_price, exit_reason, holding_days, pnl_pct) "
                "VALUES ('ps_p1', 'v1', 'close_on_publish_date', 'full', :entry, 100, "
                ":exit, 105, 'fixed_tp', 3, 5.0)"
            ),
            {"entry": entry, "exit": exit_win},
        )
        # 측정 성공 — 손절 -4% (5일 보유)
        await session.execute(
            text(
                "INSERT INTO paper_outcomes "
                "(sheet_id, simulator_version, entry_model, coverage, entry_date, entry_price, "
                "exit_date, exit_price, exit_reason, holding_days, pnl_pct) "
                "VALUES ('ps_p2', 'v1', 'close_on_publish_date', 'daily_only', :entry, 50000, "
                ":exit, 48000, 'fixed_sl', 5, -4.0)"
            ),
            {"entry": entry, "exit": exit_lose},
        )
        # 측정 실패 (data_missing)
        await session.execute(
            text(
                "INSERT INTO paper_outcomes "
                "(sheet_id, simulator_version, entry_model, coverage, entry_date, entry_price, "
                "exit_reason) "
                "VALUES ('ps_p3', 'v1', 'close_on_publish_date', 'daily_only', :entry, NULL, "
                "'data_missing')"
            ),
            {"entry": entry},
        )

        # 벤치마크 (KODEX200) 일봉 — entry 10000, 3일 후 +1% (10100), 5일 후 -1% (9900)
        for d, close in [(entry, 10000), (exit_win, 10100), (exit_lose, 9900)]:
            await session.execute(
                text(
                    "INSERT INTO daily_prices "
                    "(stock_code, price_date, open_price, high_price, low_price, "
                    "close_price, volume) "
                    "VALUES ('069500', :d, :c, :c, :c, :c, 1000)"
                ),
                {"d": d, "c": close},
            )
        await session.commit()


async def test_paper_outcomes_with_benchmark_alpha(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/paper/outcomes", params={"days": 30})
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 3

        by_id = {r["sheet_id"]: r for r in rows}
        win = by_id["ps_p1"]
        lose = by_id["ps_p2"]
        missing = by_id["ps_p3"]

        # 익절 시트: PnL +5%, 같은 기간 벤치마크 +1% → alpha +4%p
        assert win["pnl_pct"] == 5.0
        assert win["benchmark_pnl_pct"] == 1.0
        assert win["alpha_pct"] == 4.0
        assert win["strategy_tag"] == "GAP_UP_REBOUND"

        # 손절 시트: PnL -4%, 벤치마크 -1% → alpha -3%p
        assert lose["pnl_pct"] == -4.0
        assert lose["benchmark_pnl_pct"] == -1.0
        assert lose["alpha_pct"] == -3.0

        # data_missing: pnl/벤치마크/alpha 모두 None
        assert missing["pnl_pct"] is None
        assert missing["alpha_pct"] is None


async def test_paper_summary_aggregates(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/paper/summary")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["total_sheets_measured"] == 3
        assert body["data_missing_count"] == 1
        assert body["benchmark_ticker"] == "069500"

        # 전체: 측정 성공 2건 (승 1, 패 1) → 승률 50%, 평균 PnL +0.5%, 평균 alpha +0.5%p
        overall = body["overall"]
        assert overall["count"] == 2
        assert overall["win_rate"] == 50.0
        assert overall["avg_pnl_pct"] == 0.5
        assert overall["avg_alpha_pct"] == 0.5

        # 전략별 분해
        assert body["by_strategy"]["GAP_UP_REBOUND"]["count"] == 1
        assert body["by_strategy"]["GAP_UP_REBOUND"]["avg_pnl_pct"] == 5.0
        assert body["by_strategy"]["SECTOR_MOMENTUM"]["count"] == 1

        # 청산 사유별 분해
        assert body["by_exit_reason"]["fixed_tp"]["count"] == 1
        assert body["by_exit_reason"]["fixed_sl"]["count"] == 1

        # coverage 별 분해
        assert body["by_coverage"]["full"]["count"] == 1
        assert body["by_coverage"]["daily_only"]["count"] == 1


async def test_paper_summary_empty_db(app, session_factory):
    """측정 데이터가 하나도 없어도 빈 요약으로 정상 응답."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/paper/summary")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_sheets_measured"] == 0
        assert body["overall"]["count"] == 0
        assert body["overall"]["win_rate"] is None


async def test_paper_outcomes_benchmark_missing_dates(app, session_factory):
    """벤치마크 일봉이 없는 날짜의 시트는 alpha=None (측정 자체는 표시)."""
    now = datetime.now(UTC)
    entry = date.today() - timedelta(days=5)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO position_sheets "
                "(sheet_id, schema_version, generated_at, valid_until, ticker, "
                "strategy_tag, sheet_json, provenance_json) "
                "VALUES ('ps_nb', '1.1', :g, :v, '005930', 'GAP_UP_REBOUND', '{}', '{}')"
            ),
            {"g": now, "v": now + timedelta(hours=5)},
        )
        await session.execute(
            text(
                "INSERT INTO paper_outcomes "
                "(sheet_id, simulator_version, entry_model, coverage, entry_date, entry_price, "
                "exit_date, exit_price, exit_reason, holding_days, pnl_pct) "
                "VALUES ('ps_nb', 'v1', 'close_on_publish_date', 'full', :entry, 100, "
                ":exit, 103, 'fixed_tp', 2, 3.0)"
            ),
            {"entry": entry, "exit": entry + timedelta(days=2)},
        )
        # 벤치마크 일봉 없음.
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/paper/outcomes")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["pnl_pct"] == 3.0
        assert rows[0]["benchmark_pnl_pct"] is None
        assert rows[0]["alpha_pct"] is None
