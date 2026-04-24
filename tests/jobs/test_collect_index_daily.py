"""`collect_index_daily_prices` 스모크 — fchart XML 응답 모킹 + upsert 검증."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.market_data import collect_index_daily_prices

_FCHART_URL_RE = r"https://fchart\.stock\.naver\.com/sise\.nhn.*"


def _fchart_xml(bars: list[tuple[str, float, float, float, float, int]]) -> str:
    items = "".join(f'<item data="{d}|{o}|{hi}|{lo}|{c}|{v}" />' for d, o, hi, lo, c, v in bars)
    return f'<?xml version="1.0" encoding="UTF-8"?><protocol>{items}</protocol>'


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_collect_index_daily_upserts_with_change_pct():
    pool = _FakePool()
    kospi_bars = [
        ("20240102", 3000.0, 3050.0, 2990.0, 3020.0, 100_000),
        ("20240103", 3020.0, 3060.0, 3010.0, 3040.0, 120_000),
    ]
    kosdaq_bars = [
        ("20240102", 900.0, 910.0, 895.0, 905.0, 50_000),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FCHART_URL_RE).mock(
            side_effect=[
                httpx.Response(200, text=_fchart_xml(kospi_bars)),
                httpx.Response(200, text=_fchart_xml(kosdaq_bars)),
            ]
        )
        async with httpx.AsyncClient() as client:
            await collect_index_daily_prices(pool, client, days=2)

    inserts = [c for c in pool.conn.calls if "INSERT INTO index_daily_prices" in c[0]]
    assert len(inserts) == 3

    # 첫 KOSPI 바는 change_pct=None, 두 번째는 (3040-3020)/3020 * 100
    kospi_calls = [c for c in inserts if c[1][0] == "KOSPI"]
    assert kospi_calls[0][1][7] is None
    assert abs(kospi_calls[1][1][7] - ((3040 - 3020) / 3020 * 100)) < 1e-9


@pytest.mark.asyncio
async def test_collect_index_daily_skips_empty_response(caplog):
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FCHART_URL_RE).respond(
            200, text='<?xml version="1.0"?><protocol></protocol>'
        )
        async with httpx.AsyncClient() as client:
            await collect_index_daily_prices(pool, client, days=10)

    assert pool.conn.calls == []
