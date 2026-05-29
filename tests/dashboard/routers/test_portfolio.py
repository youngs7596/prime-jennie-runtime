"""Portfolio 라우터 happy path — KIS Gateway mock + outcomes 성과."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text


async def test_summary_with_kis_mock(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/balance$").mock(
            return_value=Response(
                200,
                json={
                    "cash_balance": 1_000_000,
                    "total_asset": 3_500_000,
                    "stock_eval_amount": 2_500_000,
                    "positions": [
                        {
                            "stock_code": "005930",
                            "stock_name": "삼성전자",
                            "quantity": 10,
                            "average_buy_price": 70000.0,
                            "total_buy_amount": 700000.0,
                            "current_price": 72000.0,
                            "current_value": 720000.0,
                            "profit_pct": 2.86,
                        }
                    ],
                },
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/portfolio/summary")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["position_count"] == 1
            assert body["cash_balance"] == 1_000_000
            assert body["positions"][0]["stock_code"] == "005930"


async def test_history_from_daily_asset_snapshots(app, session_factory):
    """`/api/portfolio/history` 가 daily_asset_snapshots 테이블 행을 시간순으로 반환."""
    # /history 는 'now - days' 창으로 필터하므로 날짜를 오늘 기준 상대값으로 찍는다
    # (고정 과거 날짜로 찍으면 실행일이 지나며 창 밖으로 빠져 0 행 — 구 stale 원인).
    today = datetime.now(UTC).date()
    d0, d1, d2 = (today - timedelta(days=n) for n in (3, 2, 1))
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO daily_asset_snapshots "
                "(snapshot_date, total_asset, cash_balance, stock_eval_amount, "
                "position_count, total_profit_loss, realized_profit_loss) "
                "VALUES "
                f"('{d0.isoformat()}', 200000000, 50000000, 150000000, 3, -1000000, 0), "
                f"('{d1.isoformat()}', 198000000, 50000000, 148000000, 3, -3000000, -500000), "
                f"('{d2.isoformat()}', 199500000, 50000000, 149500000, 3, -1500000, 0)"
            )
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/portfolio/history?days=30")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 3
        # 시간 오름차순
        dates = [r["snapshot_date"][:10] for r in body]
        assert dates == [d0.isoformat(), d1.isoformat(), d2.isoformat()]
        assert body[1]["realized_profit_loss"] == -500000
        assert body[2]["total_asset"] == 199500000


async def test_summary_sector_group_and_etf_classification(app, session_factory):
    """positions 응답의 sector_group: stock_masters lookup + ETF prefix 분기."""
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO stock_masters "
                "(stock_code, stock_name, market, sector_group) "
                "VALUES "
                "('005930', '삼성전자', 'KOSPI', '반도체/IT'), "
                "('051910', 'LG화학', 'KOSPI', '2차전지/소재'), "
                "('069500', 'KODEX 200', 'KOSPI', '기타')"
            )
        )
        await session.commit()

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/balance$").mock(
            return_value=Response(
                200,
                json={
                    "cash_balance": 0,
                    "total_asset": 0,
                    "stock_eval_amount": 0,
                    "positions": [
                        {
                            "stock_code": "005930",
                            "stock_name": "삼성전자",
                            "quantity": 10,
                            "average_buy_price": 70000.0,
                            "total_buy_amount": 700000.0,
                        },
                        {
                            "stock_code": "069500",
                            "stock_name": "KODEX 200",
                            "quantity": 100,
                            "average_buy_price": 35000.0,
                            "total_buy_amount": 3500000.0,
                        },
                        {
                            "stock_code": "999999",
                            "stock_name": None,
                            "quantity": 5,
                            "average_buy_price": 1000.0,
                            "total_buy_amount": 5000.0,
                        },
                    ],
                },
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/portfolio/summary")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            by_code = {p["stock_code"]: p for p in body["positions"]}
            # stock_masters 의 sector_group 그대로
            assert by_code["005930"]["sector_group"] == "반도체/IT"
            # ETF prefix 우선 — stock_masters 값 무시되고 'ETF'
            assert by_code["069500"]["sector_group"] == "ETF"
            # stock_masters 에 없고 ETF 도 아니면 None
            assert by_code["999999"]["sector_group"] is None


async def test_history_empty_when_no_rows(app):
    """daily_asset_snapshots 비어있으면 빈 리스트."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/portfolio/history?days=30")
        assert resp.status_code == 200
        assert resp.json() == []


async def test_performance_from_outcomes(app, session_factory):
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO position_sheets "
                "(sheet_id, schema_version, generated_at, valid_until, ticker, strategy_tag, sheet_json, provenance_json) "
                "VALUES ('ps_p1', '1.1', :g, :v, '005930', 'gap_up', '{}', '{}'), "
                "('ps_p2', '1.1', :g, :v, '035720', 'breakout', '{}', '{}')"
            ),
            {"g": now - timedelta(days=1), "v": now + timedelta(hours=4)},
        )
        await session.execute(
            text(
                "INSERT INTO outcomes (sheet_id, entry_price, exit_price, pnl_krw, pnl_pct, exit_reason, closed_at) "
                "VALUES ('ps_p1', 70000, 72000, 20000, 2.86, 'tp', :at), "
                "('ps_p2', 50000, 48000, -20000, -4.0, 'sl', :at)"
            ),
            {"at": now - timedelta(hours=2)},
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/portfolio/performance?days=7")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_trades"] == 2
        assert body["win_trades"] == 1
        assert body["loss_trades"] == 1
        assert body["total_profit"] == 0
