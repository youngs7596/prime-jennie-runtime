"""수집 잡이 한 건도 못 건졌을 때 실패로 신고하는지 — 2026-09-10 침묵 회귀.

네이버 사이트 이전 때 크롤러가 전 종목 None 을 돌려줬는데도 잡은 success 로
끝났고, 엿새 동안 아무도 몰랐다. 대상이 있는데 수확이 0 이면 예외를 올린다.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs import fundamentals as fmod
from prime_jennie_runtime.jobs import investor_data as imod
from prime_jennie_runtime.jobs.collection_guard import (
    CollectionSourceDeadError,
    raise_if_nothing_collected,
)

_FINANCE_URL_RE = r"https://m\.stock\.naver\.com/api/stock/\w+/finance/quarter"
_TREND_URL_RE = r"https://m\.stock\.naver\.com/api/stock/\w+/trend.*"


def test_guard_passes_when_something_collected():
    raise_if_nothing_collected("job", collected=1, candidates=300)


def test_guard_passes_when_no_candidates():
    """대상 자체가 없으면(휴장·빈 유니버스) 판단하지 않는다."""
    raise_if_nothing_collected("job", collected=0, candidates=0)


def test_guard_raises_when_nothing_collected():
    with pytest.raises(CollectionSourceDeadError, match="한 건도"):
        raise_if_nothing_collected("job", collected=0, candidates=300)


class _FakeConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        return [{"stock_code": "005930"}, {"stock_code": "000660"}]

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
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
async def test_quarterly_financials_raises_when_source_dead():
    """이전된 옛 주소처럼 본문이 JSON 이 아니면 잡이 실패로 끝나야 한다."""
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FINANCE_URL_RE).respond(200, text="<!DOCTYPE html>")
        async with httpx.AsyncClient() as client:
            with pytest.raises(CollectionSourceDeadError):
                await fmod.collect_quarterly_financials(pool, client, throttle_sec=0.0)
    assert pool.conn.execute_calls == []


@pytest.mark.asyncio
async def test_investor_trading_raises_when_source_dead():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_TREND_URL_RE).respond(200, text="<!DOCTYPE html>")
        async with httpx.AsyncClient() as client:
            with pytest.raises(CollectionSourceDeadError):
                await imod.collect_investor_trading(pool, client, throttle_sec=0.0)
    assert pool.conn.execute_calls == []
