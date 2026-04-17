"""`collect_investor_trading` + `collect_foreign_holding` 스모크.

네이버 frgn 페이지 (euc-kr) 응답을 respx 로 모킹하고 stock_investor_tradings 의
upsert 가 v2 와 동일한 의미 (volume × close_price = KRW) 를 가지는지 검증.
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

_FRGN_URL_RE = r"https://finance\.naver\.com/item/frgn\.naver.*"


def _frgn_html(rows: list[tuple[str, int, int, int, float]]) -> bytes:
    """rows: (yyyy.mm.dd, close, inst_net_vol, frgn_net_vol, frgn_ratio).

    table.type2 두 번째 (summary 에 '외국인' 포함) 만 파싱되므로 첫 dummy 도 둠.
    """
    body_rows = "".join(
        f"<tr><td>{d}</td><td>{c:,}</td><td>0</td><td>0</td><td>0</td>"
        f"<td>{ins:+,}</td><td>{frg:+,}</td><td>0</td><td>{ratio:.2f}%</td></tr>"
        for d, c, ins, frg, ratio in rows
    )
    html = (
        "<html><body>"
        '<table class="type2" summary="시세">dummy</table>'
        '<table class="type2" summary="외국인 기관 순매매 거래량">'
        f"{body_rows}"
        "</table></body></html>"
    )
    return html.encode("euc-kr")


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
        mock.get(url__regex=_FRGN_URL_RE).respond(
            200, content=_frgn_html(rows), headers={"content-type": "text/html"}
        )
        async with httpx.AsyncClient() as client:
            await collect_investor_trading(pool, client, throttle_sec=0.0)

    inserts = [
        c
        for c in pool.conn.execute_calls
        if "INSERT INTO stock_investor_tradings" in c[0]
    ]
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
        mock.get(url__regex=_FRGN_URL_RE).respond(
            200, content=_frgn_html(rows), headers={"content-type": "text/html"}
        )
        async with httpx.AsyncClient() as client:
            await collect_foreign_holding(pool, client, throttle_sec=0.0)

    inserts = [
        c
        for c in pool.conn.execute_calls
        if "INSERT INTO stock_investor_tradings" in c[0]
    ]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == "005930"
    assert args[1] == date(2026, 4, 16)
    assert abs(args[2] - 51.23) < 1e-9


@pytest.mark.asyncio
async def test_collect_investor_trading_skips_when_fetch_empty():
    pool = _FakePool(["005930"])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FRGN_URL_RE).respond(
            200, content=b"<html></html>", headers={"content-type": "text/html"}
        )
        async with httpx.AsyncClient() as client:
            await collect_investor_trading(pool, client, throttle_sec=0.0)

    assert pool.conn.execute_calls == []
