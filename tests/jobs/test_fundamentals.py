"""`collect_consensus` / `collect_naver_roe` / `collect_quarterly_financials` 스모크.

크롤러 (fnguide / naver) 는 직접 monkeypatch 해서 SQL 흐름만 검증.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from prime_jennie_runtime.jobs import fundamentals as fmod
from prime_jennie_runtime.jobs.crawlers.fnguide import ConsensusData


class _FakeConn:
    def __init__(self, codes: list[str]) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self._codes = codes

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetch_calls.append((sql, args))
        return [{"stock_code": c} for c in self._codes]

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, codes: list[str]) -> None:
        self.conn = _FakeConn(codes)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_collect_consensus_upserts_with_source(monkeypatch):
    pool = _FakePool(["005930", "000660"])

    async def fake_consensus(client, code):
        if code == "005930":
            return ConsensusData(
                forward_per=12.5,
                forward_eps=8000.0,
                forward_roe=15.2,
                target_price=85000,
                analyst_count=15,
                investment_opinion=4.2,
                source="FNGUIDE",
            )
        return ConsensusData(
            forward_per=10.0,
            forward_eps=5000.0,
            forward_roe=12.0,
            target_price=120000,
            analyst_count=8,
            investment_opinion=3.8,
            source="NAVER",
        )

    monkeypatch.setattr(fmod, "crawl_consensus", fake_consensus)

    async with httpx.AsyncClient() as client:
        await fmod.collect_consensus(pool, client, throttle_sec=0.0)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_consensus" in c[0]]
    assert len(inserts) == 2
    samsung = next(c for c in inserts if c[1][0] == "005930")
    assert samsung[1][1] == date.today()
    assert samsung[1][2] == 12.5  # forward_per
    assert samsung[1][8] == "FNGUIDE"  # source
    sk = next(c for c in inserts if c[1][0] == "000660")
    assert sk[1][8] == "NAVER"


@pytest.mark.asyncio
async def test_collect_consensus_skips_when_crawler_returns_none(monkeypatch):
    pool = _FakePool(["005930"])

    async def fake_consensus(client, code):
        return None

    monkeypatch.setattr(fmod, "crawl_consensus", fake_consensus)

    async with httpx.AsyncClient() as client:
        await fmod.collect_consensus(pool, client, throttle_sec=0.0)

    assert pool.conn.execute_calls == []


@pytest.mark.asyncio
async def test_collect_naver_roe_upserts_only_roe(monkeypatch):
    pool = _FakePool(["005930"])

    async def fake_roe(client, code):
        return 18.5

    monkeypatch.setattr(fmod, "crawl_naver_roe", fake_roe)

    async with httpx.AsyncClient() as client:
        await fmod.collect_naver_roe(pool, client, throttle_sec=0.0)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_fundamentals" in c[0]]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == "005930"
    assert args[1] == date.today()
    assert args[2] == 18.5
    # SQL 은 roe 1개만 INSERT (per/pbr 는 미포함)
    assert "(stock_code, trade_date, roe)" in inserts[0][0]


@pytest.mark.asyncio
async def test_collect_quarterly_financials_upserts_per_pbr_roe(monkeypatch):
    pool = _FakePool(["005930"])

    class _F:
        per = 12.5
        pbr = 1.3
        roe = 15.2

    async def fake_fund(client, code):
        return _F()

    monkeypatch.setattr(fmod, "crawl_naver_fundamentals", fake_fund)

    async with httpx.AsyncClient() as client:
        await fmod.collect_quarterly_financials(pool, client, throttle_sec=0.0)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_fundamentals" in c[0]]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[2] == 12.5
    assert args[3] == 1.3
    assert args[4] == 15.2
    # COALESCE 패턴으로 None 컬럼 보존하는지 SQL 검증
    assert "COALESCE(EXCLUDED.per, stock_fundamentals.per)" in inserts[0][0]


@pytest.mark.asyncio
async def test_collect_quarterly_financials_handles_partial_none(monkeypatch):
    pool = _FakePool(["005930"])

    class _F:
        per = 12.5
        pbr = None
        roe = 15.2

    async def fake_fund(client, code):
        return _F()

    monkeypatch.setattr(fmod, "crawl_naver_fundamentals", fake_fund)

    async with httpx.AsyncClient() as client:
        await fmod.collect_quarterly_financials(pool, client, throttle_sec=0.0)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_fundamentals" in c[0]]
    args = inserts[0][1]
    assert args[3] is None  # pbr → COALESCE 가 기존값 보존
