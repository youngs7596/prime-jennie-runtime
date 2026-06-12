"""`collect_minute_chart` — gateway POST + repo upsert + 자동 universe + 적응형 페이싱."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.minute_chart import (
    collect_minute_chart,
    compute_base_interval,
)

GATEWAY = "http://kis-gateway:8080"
_MINUTE_RE = rf"{GATEWAY}/api/market/minute-prices"

# 테스트는 페이싱 sleep 없이 — 창/하한 0 이면 간격 0.
_NO_PACING = {"window_sec": 0.0, "min_interval_sec": 0.0}


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
        self.seen_sqls: list[str] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.seen_sqls.append(sql)
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
    """acquire (대상 조회) + executemany (repo 가 pool 직접 사용) 둘 다 지원."""

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

    async def executemany(self, sql: str, args: list[tuple]) -> None:
        await self.conn.executemany(sql, args)


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


def _req_code(req) -> str:
    return json.loads(req.content)["stock_code"]


# ---------- 간격 연산 ----------


def test_compute_base_interval_spreads_over_window():
    # 45종목 → 240/45 = 5.33초, 100종목 → 2.4초 — 창에 고르게 분산
    assert compute_base_interval(45) == pytest.approx(240.0 / 45)
    assert compute_base_interval(100) == pytest.approx(2.4)
    # 대상이 아주 많으면 하한 (0.35초) 이 이긴다 — KIS 안전 우선
    assert compute_base_interval(10_000) == pytest.approx(0.35)
    # 빈 대상은 0
    assert compute_base_interval(0) == 0.0


# ---------- 수집 흐름 ----------


@pytest.mark.asyncio
async def test_collect_minute_chart_fetches_top_n_and_pending_sheets():
    conn = _FakeConn(
        top_codes=["005930", "000660"],
        pending_sheet_codes=["005930", "035720"],  # 005930 dup, 035720 추가
    )
    pool = _FakePool(conn)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).mock(
            side_effect=lambda req: httpx.Response(200, json=_minute_payload(_req_code(req)))
        )
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=2, **_NO_PACING)

    assert result["top_n"] == 2
    assert result["pending_sheets_added"] == 1  # 035720 only
    assert result["target"] == 3
    assert result["upserted"] == 9  # 3 종목 × 3 봉
    assert result["failed"] == 0
    assert result["backoffs"] == 0
    assert len(conn.executemany_calls) == 3  # 종목별 1회 upsert
    # 시총 상위 조회는 KOSPI 보통주·우선주만 — 코스닥·ETF 차단 (No-KOSDAQ 정책)
    top_sql = next(s for s in conn.seen_sqls if "FROM stock_masters" in s)
    assert "market = 'KOSPI'" in top_sql
    assert "security_type = 'STOCK'" in top_sql


@pytest.mark.asyncio
async def test_collect_minute_chart_handles_no_pending_sheets():
    conn = _FakeConn(top_codes=["005930"], pending_sheet_codes=[])
    pool = _FakePool(conn)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).respond(200, json=_minute_payload("005930", n=2))
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=1, **_NO_PACING)

    assert result["target"] == 1
    assert result["pending_sheets_added"] == 0
    assert result["upserted"] == 2


# ---------- 적응형 페이싱 (혼잡 감속 + 말미 재시도) ----------


@pytest.mark.asyncio
async def test_throttle_retried_once_then_failed():
    """항상 500 인 종목 — 감속 + 말미 1회 재시도 후 failed, 나머지는 계속 수집."""
    conn = _FakeConn(top_codes=["005930", "000660"], pending_sheet_codes=[])
    pool = _FakePool(conn)
    calls: list[str] = []

    def _side(req):
        code = _req_code(req)
        calls.append(code)
        if code == "000660":
            return httpx.Response(500, json={"error": "EGW00201"})
        return httpx.Response(200, json=_minute_payload(code, n=1))

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).mock(side_effect=_side)
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=2, **_NO_PACING)

    assert result["target"] == 2
    assert result["failed"] == 1
    assert result["upserted"] == 1
    assert result["backoffs"] == 1  # 첫 거부만 감속, 재시도 실패는 failed 처리
    assert calls == ["000660", "005930", "000660"]  # 말미 재시도 1회


@pytest.mark.asyncio
async def test_transient_throttle_recovers_on_retry():
    """일시 거부 — 말미 재시도에서 성공하면 failed 0."""
    conn = _FakeConn(top_codes=["005930", "000660"], pending_sheet_codes=[])
    pool = _FakePool(conn)
    seen: dict[str, int] = {}

    def _side(req):
        code = _req_code(req)
        seen[code] = seen.get(code, 0) + 1
        if code == "000660" and seen[code] == 1:
            return httpx.Response(500, json={"error": "EGW00201"})
        return httpx.Response(200, json=_minute_payload(code, n=1))

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).mock(side_effect=_side)
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=2, **_NO_PACING)

    assert result["failed"] == 0
    assert result["upserted"] == 2
    assert result["backoffs"] == 1


@pytest.mark.asyncio
async def test_non_throttle_error_not_retried():
    """4xx (혼잡 아님) 는 재시도 없이 failed — 호출 1회뿐."""
    conn = _FakeConn(top_codes=["005930", "000660"], pending_sheet_codes=[])
    pool = _FakePool(conn)
    calls: list[str] = []

    def _side(req):
        code = _req_code(req)
        calls.append(code)
        if code == "000660":
            return httpx.Response(404, json={"error": "unknown code"})
        return httpx.Response(200, json=_minute_payload(code, n=1))

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_MINUTE_RE).mock(side_effect=_side)
        async with httpx.AsyncClient() as client:
            result = await collect_minute_chart(pool, client, GATEWAY, top_n=2, **_NO_PACING)

    assert result["failed"] == 1
    assert result["backoffs"] == 0
    assert calls.count("000660") == 1
