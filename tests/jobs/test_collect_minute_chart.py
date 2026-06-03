"""`collect_minute_chart` 스모크 — gateway POST + repo upsert + 자동 universe."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.minute_chart import collect_minute_chart

GATEWAY = "http://kis-gateway:8080"
_MINUTE_RE = rf"{GATEWAY}/api/market/minute-prices"


class _FakeConn:
    def __init__(
        self,
        *,
        top_codes: list[str],
        pending_sheet_codes: list[str],
    ) -> None:
        self.top_codes = top_codes
        # 측정 대기 시트 (paper_outcomes 미적재) 의 ticker.
        self.pending_sheet_codes = pending_sheet_codes
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM stock_masters" in sql:
            return [{"stock_code": c} for c in self.top_codes]
        if "FROM position_sheets" in sql:
            return [{"stock_code": c} for c in self.pending_sheet_codes]
        return []

    async def fetchval(self, sql: str, *args: object):
        return None

    async def execute(self, sql: str, *args: object) -> str:
        return "OK"

    async def executemany(self, sql: str, args: list[tuple]) -> None:
        self.executemany_calls.append((sql, args))


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _minute_payload(code: str, n: int = 3) -> list[dict]:
    return [
        {
            "stock_code": code,
            "price_datetime": (datetime.now() - timedelta(minutes=5 * i)).isoformat(),
            "open_price": 1000,
            "high_price": 1010,
            "low_price": 990,
            "close_price": 1005,
            "volume": 100_000,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_collect_minute_chart_fetches_top_n_and_pending_sheets():
    conn = _FakeConn(
        top_codes=["005930", "000660"],
        pending_sheet_codes=["005930", "035720"],  # 005930 dup, 035720 추가
    )
    pool = _FakePool(conn)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).mock(
            side_effect=lambda req: httpx.Response(
                200, json=_minute_payload(req.url.params.get("stock_code") or "005930")
            )
        )
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=2)

    assert result["top_n"] == 2
    assert result["pending_sheets_added"] == 1  # 035720 only
    assert result["target"] == 3
    assert result["upserted"] == 9  # 3 종목 × 3 봉
    assert result["failed"] == 0
    assert len(conn.executemany_calls) == 3  # 종목별 1회 upsert


@pytest.mark.asyncio
async def test_collect_minute_chart_handles_no_pending_sheets():
    conn = _FakeConn(top_codes=["005930"], pending_sheet_codes=[])
    pool = _FakePool(conn)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).respond(200, json=_minute_payload("005930", n=2))
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=1)

    assert result["target"] == 1
    assert result["pending_sheets_added"] == 0
    assert result["upserted"] == 2


@pytest.mark.asyncio
async def test_collect_minute_chart_continues_on_per_stock_failure():
    conn = _FakeConn(top_codes=["005930", "000660"], pending_sheet_codes=[])
    pool = _FakePool(conn)

    # 첫 종목은 500, 두 번째는 OK
    call_count = {"n": 0}

    def _side(req):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, json={"error": "ban"})
        return httpx.Response(200, json=_minute_payload("000660", n=1))

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).mock(side_effect=_side)
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=2)

    assert result["target"] == 2
    assert result["failed"] == 1
    assert result["upserted"] == 1
