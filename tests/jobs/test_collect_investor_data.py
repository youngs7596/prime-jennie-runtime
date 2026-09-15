"""`collect_investor_trading` + `collect_foreign_holding` 스모크.

네이버 일별 수급 API 응답을 respx 로 모킹하고 stock_investor_tradings 의 upsert 가
v2 와 동일한 의미 (volume × close_price = KRW) 를 가지는지 검증. 읽는 곳은
2026-09-15 에 옛 frgn HTML 표에서 모바일 JSON API 로 바뀌었다.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.investor_data import (
    collect_foreign_holding,
    collect_investor_trading,
)

_FRGN_URL_RE = r"https://m\.stock\.naver\.com/api/stock/\w+/trend.*"


def _trend_json(rows: list[tuple[str, int, int, int, float]]) -> list[dict]:
    """rows: (yyyy.mm.dd, close, inst_net_vol, frgn_net_vol, frgn_ratio).

    API 는 숫자를 콤마·부호·% 붙은 문자열로 준다 — 그 모양 그대로 흉내 낸다.
    """
    return [
        {
            "itemCode": "005930",
            "bizdate": d.replace(".", ""),
            "closePrice": f"{c:,}",
            "organPureBuyQuant": f"{ins:+,}",
            "foreignerPureBuyQuant": f"{frg:+,}",
            "foreignerHoldRatio": f"{ratio:.2f}%",
        }
        for d, c, ins, frg, ratio in rows
    ]


class _FakeConn:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_result: list[dict] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetch_calls.append((sql, args))
        return list(self.fetch_result)

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, codes: list[str]) -> None:
        self.conn = _FakeConn()
        self.conn.fetch_result = [{"stock_code": c} for c in codes]

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_collect_investor_trading_sums_recent_seven_bars():
    pool = _FakePool(["005930"])
    today = date.today()
    # 최근 3일치만 — cutoff (today-14) 이내
    rows = [
        ((today - timedelta(days=1)).strftime("%Y.%m.%d"), 70000, 1000, 2000, 50.0),
        ((today - timedelta(days=2)).strftime("%Y.%m.%d"), 71000, -500, 1000, 50.1),
        ((today - timedelta(days=3)).strftime("%Y.%m.%d"), 72000, 300, -700, 50.2),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FRGN_URL_RE).respond(200, json=_trend_json(rows))
        async with httpx.AsyncClient() as client:
            await collect_investor_trading(pool, client, throttle_sec=0.0)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_investor_tradings" in c[0]]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == "005930"
    assert args[1] == today
    # foreign_net = 2000*70000 + 1000*71000 + (-700)*72000 = 140M + 71M - 50.4M
    expected_frgn = 2000 * 70000 + 1000 * 71000 + (-700) * 72000
    expected_inst = 1000 * 70000 + (-500) * 71000 + 300 * 72000
    assert args[2] == float(expected_frgn)
    assert args[3] == float(expected_inst)


@pytest.mark.asyncio
async def test_collect_foreign_holding_uses_latest_row_date():
    pool = _FakePool(["005930"])
    rows = [
        ("2026.04.16", 70000, 0, 0, 51.23),
        ("2026.04.15", 70500, 0, 0, 51.10),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FRGN_URL_RE).respond(200, json=_trend_json(rows))
        async with httpx.AsyncClient() as client:
            await collect_foreign_holding(pool, client, throttle_sec=0.0)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_investor_tradings" in c[0]]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == "005930"
    assert args[1] == date(2026, 4, 16)
    assert abs(args[2] - 51.23) < 1e-9


@pytest.mark.asyncio
async def test_collect_investor_trading_skips_when_fetch_empty():
    pool = _FakePool(["005930"])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FRGN_URL_RE).respond(200, json=[])
        async with httpx.AsyncClient() as client:
            await collect_investor_trading(pool, client, throttle_sec=0.0)

    assert pool.conn.execute_calls == []
