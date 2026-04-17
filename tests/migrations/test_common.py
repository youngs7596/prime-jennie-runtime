"""공통 헬퍼 단위 테스트 — row_hash, table_hash, copy_rows, build_insert_sql."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from migrations._common import (
    LegacyRowSource,
    MigrationStats,
    TargetWriter,
    build_insert_sql,
    copy_rows,
    row_hash,
    table_hash,
)

from ._fakes import InMemorySource, InMemoryTarget


def _rows(*ids: int) -> list[dict]:
    return [
        {
            "id": i,
            "stock_code": f"{i:06d}",
            "price": 100 * i,
            "note": None,
            "created_at": datetime(2026, 4, 1, 9, 0, i % 60),
        }
        for i in ids
    ]


COLUMNS = ["id", "stock_code", "price", "note", "created_at"]


# ---------- row_hash ----------


def test_row_hash_is_deterministic():
    row = _rows(1)[0]
    h1 = row_hash(row, columns=COLUMNS)
    h2 = row_hash(dict(row), columns=COLUMNS)
    assert h1 == h2


def test_row_hash_changes_on_column_value_change():
    row = _rows(1)[0]
    h0 = row_hash(row, columns=COLUMNS)
    row["price"] = 999
    assert row_hash(row, columns=COLUMNS) != h0


def test_row_hash_treats_none_consistently():
    a = _rows(1)[0]
    a["note"] = None
    b = dict(a)
    b["note"] = ""  # None == "" 로 정규화되면 같은 해시
    assert row_hash(a, columns=COLUMNS) == row_hash(b, columns=COLUMNS)


def test_row_hash_datetime_and_date_serialized_as_isoformat():
    a = {"id": 1, "ts": datetime(2026, 4, 1, 9, 0, 0), "d": date(2026, 4, 1)}
    b = {"id": 1, "ts": "2026-04-01T09:00:00", "d": "2026-04-01"}
    cols = ["id", "ts", "d"]
    assert row_hash(a, columns=cols) == row_hash(b, columns=cols)


# ---------- table_hash ----------


def test_table_hash_is_order_independent():
    rows = _rows(1, 2, 3)
    h1 = table_hash(rows, columns=COLUMNS)
    h2 = table_hash(list(reversed(rows)), columns=COLUMNS)
    assert h1 == h2


def test_table_hash_detects_missing_row():
    full = _rows(1, 2, 3)
    partial = _rows(1, 2)
    assert table_hash(full, columns=COLUMNS) != table_hash(partial, columns=COLUMNS)


def test_table_hash_empty_list():
    empty_hash = table_hash([], columns=COLUMNS)
    assert isinstance(empty_hash, str)
    assert len(empty_hash) == 64


# ---------- build_insert_sql ----------


def test_build_insert_sql_uses_asyncpg_dollar_placeholders():
    sql = build_insert_sql(table="legacy_signal_logs", columns=["id", "price"])
    assert "INSERT INTO legacy_signal_logs (id, price)" in sql
    assert "VALUES ($1, $2)" in sql
    assert "ON CONFLICT (id) DO NOTHING" in sql


def test_build_insert_sql_respects_custom_pk():
    sql = build_insert_sql(table="legacy_foo", columns=["pk", "a"], on_conflict_pk="pk")
    assert "ON CONFLICT (pk) DO NOTHING" in sql


# ---------- copy_rows ----------


@pytest.mark.asyncio
async def test_copy_rows_basic_happy_path():
    source = InMemorySource(rows=_rows(1, 2, 3, 4, 5))
    target = InMemoryTarget()
    stats = await copy_rows(source, target, batch_size=2)
    assert stats.source_total == 5
    assert stats.copied == 5
    assert stats.skipped == 0
    assert stats.batches == 3  # 2 + 2 + 1
    assert stats.ok


@pytest.mark.asyncio
async def test_copy_rows_idempotent_on_rerun():
    """같은 id 가 다시 들어와도 target 이 skip. copy_rows 를 두 번 돌려도 무해."""
    source = InMemorySource(rows=_rows(1, 2, 3))
    target = InMemoryTarget()
    await copy_rows(source, target, batch_size=10)
    stats2 = await copy_rows(source, target, batch_size=10)
    assert stats2.source_total == 3
    assert stats2.copied == 0
    assert stats2.skipped == 3
    assert await target.count() == 3  # 중복 없음


@pytest.mark.asyncio
async def test_copy_rows_dry_run_reads_but_does_not_write():
    source = InMemorySource(rows=_rows(1, 2, 3))
    target = InMemoryTarget()
    stats = await copy_rows(source, target, batch_size=10, dry_run=True)
    assert stats.source_total == 3
    assert stats.copied == 0
    assert stats.batches == 1  # batch 경계는 세지만 write 는 skip
    assert await target.count() == 0


@pytest.mark.asyncio
async def test_copy_rows_since_until_filter():
    source = InMemorySource(rows=_rows(1, 2, 3, 4, 5))
    target = InMemoryTarget()
    stats = await copy_rows(
        source,
        target,
        batch_size=10,
        since=datetime(2026, 4, 1, 9, 0, 2),
        until=datetime(2026, 4, 1, 9, 0, 4),
    )
    # second == 2, 3 만 통과 (>=2, <4)
    assert stats.source_total == 2
    assert stats.copied == 2


@pytest.mark.asyncio
async def test_copy_rows_captures_batch_errors_continues():
    """한 batch 가 실패해도 copy_rows 는 다음 batch 를 계속 처리."""
    source = InMemorySource(rows=_rows(1, 2, 3, 4))
    target = InMemoryTarget(fail_on_batch=1)  # 첫 batch 실패
    stats = await copy_rows(source, target, batch_size=2)
    assert not stats.ok
    assert len(stats.errors) == 1
    # 두 번째 batch (id 3, 4) 는 정상 write
    assert stats.copied == 2


@pytest.mark.asyncio
async def test_copy_rows_empty_source():
    source = InMemorySource(rows=[])
    target = InMemoryTarget()
    stats = await copy_rows(source, target, batch_size=10)
    assert stats.source_total == 0
    assert stats.copied == 0
    assert stats.batches == 0


# ---------- Protocol 적합성 ----------


def test_fakes_conform_to_protocols():
    source = InMemorySource(rows=[])
    target = InMemoryTarget()
    assert isinstance(source, LegacyRowSource)
    assert isinstance(target, TargetWriter)


def test_migration_stats_ok_only_when_no_errors():
    s = MigrationStats()
    assert s.ok
    s.errors.append("boom")
    assert not s.ok
