"""RealtimeBuffer — 묶음 flush / 버퍼 드롭 / 종료 시 잔여 flush 검증.

실제 DB 대신 copy_records_to_table 호출을 기록하는 가짜 pool 을 주입한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.kis_gateway.realtime_persist import RealtimeBuffer

_TS = datetime(2026, 6, 16, 9, 30, 15, tzinfo=UTC)


class _FakeConn:
    def __init__(self):
        self.copied: list[tuple] = []  # (table, records, columns)

    async def copy_records_to_table(self, table, *, records, columns):
        self.copied.append((table, list(records), tuple(columns)))


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _AcquireCtx(self.conn)


def _tick(code="005930"):
    return (code, _TS, 71200, 13, 123456, 1, 112.5, 71210, 71190)


def _ob(code="005930"):
    return (code, _TS, [1] * 10, [2] * 10, [3] * 10, [4] * 10, 50, 60)


async def test_flush_inserts_both_tables():
    pool = _FakePool()
    buf = RealtimeBuffer(pool, flush_interval=100)
    buf.add_tick(_tick())
    buf.add_tick(_tick())
    buf.add_orderbook(_ob())

    await buf.flush()

    tables = {c[0]: c for c in pool.conn.copied}
    assert "realtime_ticks" in tables
    assert "realtime_orderbook" in tables
    assert len(tables["realtime_ticks"][1]) == 2
    assert len(tables["realtime_orderbook"][1]) == 1


async def test_flush_empty_is_noop():
    pool = _FakePool()
    buf = RealtimeBuffer(pool, flush_interval=100)
    await buf.flush()
    assert pool.conn.copied == []


async def test_flush_clears_buffer():
    pool = _FakePool()
    buf = RealtimeBuffer(pool, flush_interval=100)
    buf.add_tick(_tick())
    await buf.flush()
    # 두 번째 flush 는 비어 있어 새 copy 가 없다.
    await buf.flush()
    assert len(pool.conn.copied) == 1


async def test_max_buffer_drops_overflow():
    pool = _FakePool()
    buf = RealtimeBuffer(pool, flush_interval=100, max_buffer=2)
    buf.add_tick(_tick())
    buf.add_tick(_tick())
    buf.add_tick(_tick())  # 드롭
    buf.add_orderbook(_ob())  # 드롭

    await buf.flush()
    assert len(pool.conn.copied[0][1]) == 2  # 틱 2건만 적재
    assert buf._dropped == 2


async def test_stop_flushes_remaining():
    pool = _FakePool()
    buf = RealtimeBuffer(pool, flush_interval=100)
    await buf.start()
    buf.add_tick(_tick())
    await buf.stop()

    assert any(c[0] == "realtime_ticks" for c in pool.conn.copied)
    assert buf._task is None


# pytest 네임스페이스 워닝 억제
_ = pytest


async def test_flush_marks_after_close_rows():
    """KST 15:31 이후 행만 after_close=TRUE, 정규장은 NULL (migration 035 와 같은 규칙)."""
    kst = timezone(timedelta(hours=9))
    pool = _FakePool()
    buf = RealtimeBuffer(pool, flush_interval=100)
    regular = datetime(2026, 10, 12, 15, 30, 59, tzinfo=kst)
    after = datetime(2026, 10, 12, 15, 31, 0, tzinfo=kst)
    buf.add_tick(("005930", regular, 71200, 13, 123456, 1, 112.5, 71210, 71190))
    buf.add_tick(("005930", after, 71200, 13, 123456, 1, 112.5, 71210, 71190))
    # UTC 로 들어와도 KST 로 판정 (06:31 UTC = 15:31 KST)
    buf.add_orderbook(("005930", datetime(2026, 10, 12, 6, 31, tzinfo=UTC), *_ob()[2:]))

    await buf.flush()

    tables = {c[0]: c for c in pool.conn.copied}
    _, ticks, tick_cols = tables["realtime_ticks"]
    _, obs, ob_cols = tables["realtime_orderbook"]
    assert tick_cols[-1] == "after_close" and ob_cols[-1] == "after_close"
    assert all(len(r) == len(tick_cols) for r in ticks)
    assert [r[-1] for r in ticks] == [None, True]
    assert obs[0][-1] is True
