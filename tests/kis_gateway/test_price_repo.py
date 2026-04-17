"""InMemoryPriceRepo 단위 테스트 — fetch / upsert / 순서."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from prime_jennie_runtime.kis_gateway.price_repo import (
    InMemoryPriceRepo,
    PriceRepo,
)
from prime_jennie_runtime.kis_gateway.schemas import DailyPrice, MinutePrice


def _d(code: str, yyyymmdd: str, close: int = 100) -> DailyPrice:
    return DailyPrice(
        stock_code=code,
        price_date=date.fromisoformat(yyyymmdd),
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=1_000,
    )


def _m(code: str, ts: str, close: int = 100) -> MinutePrice:
    return MinutePrice(
        stock_code=code,
        price_datetime=datetime.fromisoformat(ts),
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=500,
    )


def test_conforms_to_protocol():
    assert isinstance(InMemoryPriceRepo(), PriceRepo)


@pytest.mark.asyncio
async def test_upsert_then_fetch_daily_returns_newest_first():
    repo = InMemoryPriceRepo()
    await repo.upsert_daily(
        [
            _d("005930", "2026-04-10"),
            _d("005930", "2026-04-15"),
            _d("005930", "2026-04-12"),
        ]
    )
    rows = await repo.fetch_daily("005930", days=10)
    assert [r.price_date.isoformat() for r in rows] == [
        "2026-04-15",
        "2026-04-12",
        "2026-04-10",
    ]


@pytest.mark.asyncio
async def test_fetch_daily_respects_limit():
    repo = InMemoryPriceRepo()
    await repo.upsert_daily([_d("005930", f"2026-04-{d:02d}") for d in (10, 11, 12, 13)])
    rows = await repo.fetch_daily("005930", days=2)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_fetch_daily_is_per_ticker():
    repo = InMemoryPriceRepo()
    await repo.upsert_daily([_d("005930", "2026-04-10"), _d("000660", "2026-04-10")])
    rows = await repo.fetch_daily("005930", days=10)
    assert len(rows) == 1
    assert rows[0].stock_code == "005930"


@pytest.mark.asyncio
async def test_upsert_same_key_overwrites():
    repo = InMemoryPriceRepo()
    await repo.upsert_daily([_d("005930", "2026-04-10", close=100)])
    await repo.upsert_daily([_d("005930", "2026-04-10", close=999)])
    rows = await repo.fetch_daily("005930", days=10)
    assert len(rows) == 1
    assert rows[0].close_price == 999


@pytest.mark.asyncio
async def test_minute_fetch_newest_first():
    repo = InMemoryPriceRepo()
    await repo.upsert_minute(
        [_m("005930", "2026-04-10T09:00:00"), _m("005930", "2026-04-10T09:02:00")]
    )
    rows = await repo.fetch_minute("005930")
    assert rows[0].price_datetime.isoformat() == "2026-04-10T09:02:00"


@pytest.mark.asyncio
async def test_empty_upsert_returns_zero():
    repo = InMemoryPriceRepo()
    assert await repo.upsert_daily([]) == 0
    assert await repo.upsert_minute([]) == 0
