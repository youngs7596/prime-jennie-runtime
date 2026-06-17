"""`maintenance.cleanup_old_data` 단위 테스트 — fake asyncpg pool.

v2 에서는 sync SQLModel + `DELETE ... < cutoff`. v3 에서는 asyncpg. v2 와
동일한 cutoff 계산(`today - timedelta(days=365)`)과 쿼리 모양(`price_date < $1`)
을 검증.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest

from prime_jennie_runtime.jobs.maintenance import (
    DEFAULT_CLEANUP_DAYS,
    cleanup_old_data,
)


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.result: str = "DELETE 0"

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return self.result


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):  # noqa: N805
                return conn

            async def __aexit__(self, *exc):  # noqa: N805
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_cleanup_uses_365d_cutoff_by_default():
    pool = _FakePool()
    await cleanup_old_data(pool)
    # daily_prices 만 정리한다 (실시간 체결·호가는 영구 보관)
    assert len(pool.conn.calls) == 1
    sql, args = pool.conn.calls[0]
    assert "DELETE FROM daily_prices" in sql
    assert "price_date < $1" in sql
    (cutoff,) = args
    assert cutoff == date.today() - timedelta(days=DEFAULT_CLEANUP_DAYS)


@pytest.mark.asyncio
async def test_cleanup_never_touches_realtime():
    """실시간 체결·호가는 KIS 가 과거를 안 주는 휘발성 데이터라 영구 보관 —
    cleanup 이 절대 건드리면 안 된다 (2026-06-17 결정 회귀 가드)."""
    pool = _FakePool()
    await cleanup_old_data(pool)
    all_sql = " ".join(c[0] for c in pool.conn.calls)
    assert "realtime_ticks" not in all_sql
    assert "realtime_orderbook" not in all_sql


@pytest.mark.asyncio
async def test_cleanup_honours_custom_days():
    pool = _FakePool()
    await cleanup_old_data(pool, days=30)
    (_, args) = pool.conn.calls[0]
    (cutoff,) = args
    assert cutoff == date.today() - timedelta(days=30)


@pytest.mark.asyncio
async def test_cleanup_parses_deleted_count_from_tag(caplog):
    pool = _FakePool()
    pool.conn.result = "DELETE 42"
    with caplog.at_level(logging.INFO, logger="prime_jennie_runtime.jobs.maintenance"):
        await cleanup_old_data(pool)
    assert any("del=42" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_cleanup_tolerates_unparseable_tag(caplog):
    pool = _FakePool()
    pool.conn.result = "UNEXPECTED"
    with caplog.at_level(logging.INFO, logger="prime_jennie_runtime.jobs.maintenance"):
        await cleanup_old_data(pool)
    assert any("del=0" in rec.message for rec in caplog.records)
