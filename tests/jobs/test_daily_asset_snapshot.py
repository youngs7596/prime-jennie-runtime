"""`daily_asset_snapshot` 스모크 — KIS gateway + outcomes 모킹."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.asset_snapshot import daily_asset_snapshot

GATEWAY = "http://kis-gateway:8080"
_BAL_RE = rf"{GATEWAY}/api/balance"


class _FakeConn:
    def __init__(self, realized: int = 0) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self._realized = realized

    async def fetchrow(self, sql: str, *args: object) -> dict:
        self.fetchrow_calls.append((sql, args))
        return {"realized": self._realized}

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, realized: int = 0) -> None:
        self.conn = _FakeConn(realized)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _bal(total: int, cash: int, eval_: int, positions: list[dict]) -> dict:
    return {
        "positions": positions,
        "cash_balance": cash,
        "total_asset": total,
        "stock_eval_amount": eval_,
        "position_count": len(positions),
        "timestamp": datetime.now().isoformat(),
    }


@pytest.mark.asyncio
async def test_daily_asset_snapshot_upserts_with_pnl():
    pool = _FakePool(realized=500_000)
    payload = _bal(
        total=10_000_000,
        cash=2_000_000,
        eval_=8_000_000,
        positions=[
            {
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "quantity": 100,
                "average_buy_price": 70_000,
                "total_buy_amount": 7_000_000,
                "current_price": 75_000,
                "current_value": 7_500_000,
                "profit_pct": 7.14,
            }
        ],
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_BAL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            await daily_asset_snapshot(pool, client, GATEWAY)

    inserts = [
        c for c in pool.conn.execute_calls if "INSERT INTO daily_asset_snapshots" in c[0]
    ]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == date.today()
    assert args[1] == 10_000_000  # total
    assert args[2] == 2_000_000   # cash
    assert args[3] == 8_000_000   # stock_eval
    assert args[4] == 1            # position_count
    # total_pnl = unrealized (7,500,000 - 7,000,000 = 500,000) + realized (500,000) = 1,000,000
    assert args[5] == 1_000_000
    assert args[6] == 500_000


@pytest.mark.asyncio
async def test_daily_asset_snapshot_falls_back_when_total_zero():
    pool = _FakePool(realized=0)
    payload = _bal(total=0, cash=3_000_000, eval_=4_000_000, positions=[])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_BAL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            await daily_asset_snapshot(pool, client, GATEWAY)

    args = pool.conn.execute_calls[0][1]
    assert args[1] == 7_000_000  # cash + stock_eval fallback
    assert args[4] == 0  # position_count


@pytest.mark.asyncio
async def test_daily_asset_snapshot_handles_empty_positions():
    pool = _FakePool(realized=0)
    payload = _bal(total=5_000_000, cash=5_000_000, eval_=0, positions=[])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_BAL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            await daily_asset_snapshot(pool, client, GATEWAY)

    args = pool.conn.execute_calls[0][1]
    assert args[5] == 0  # total_pnl
    assert args[6] == 0  # realized_pnl
