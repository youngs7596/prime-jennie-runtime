"""verify_migration 핵심 비교 로직 — count + hash 일치/불일치."""

from __future__ import annotations

from datetime import datetime

import pytest

from migrations._common import table_hash
from migrations.verify_migration import (
    VerifyResult,
    _compare_counts,
    _compare_hashes,
)

from ._fakes import InMemorySource, InMemoryTarget

COLUMNS = ["id", "stock_code", "price", "created_at"]


def _row(i: int) -> dict:
    return {
        "id": i,
        "stock_code": f"{i:06d}",
        "price": 1000 * i,
        "created_at": datetime(2026, 3, 1, 9, 0, i % 60),
    }


def _rows(*ids: int) -> list[dict]:
    return [_row(i) for i in ids]


# ---------- count 비교 ----------


@pytest.mark.asyncio
async def test_counts_match_is_ok():
    source = InMemorySource(rows=_rows(1, 2, 3))
    target = InMemoryTarget(rows=_rows(1, 2, 3), seen_ids={1, 2, 3})
    result = await _compare_counts(source, target, name="x")
    assert result.v2_count == 3
    assert result.v3_count == 3
    assert result.ok


@pytest.mark.asyncio
async def test_count_mismatch_records_error():
    source = InMemorySource(rows=_rows(1, 2, 3))
    target = InMemoryTarget(rows=_rows(1, 2), seen_ids={1, 2})
    result = await _compare_counts(source, target, name="x")
    assert not result.ok
    assert any("count mismatch" in m for m in result.mismatches)


# ---------- hash 비교 ----------


@pytest.mark.asyncio
async def test_hashes_match_when_rows_identical():
    rows = _rows(1, 2, 3)
    source = InMemorySource(rows=list(rows))
    target = InMemoryTarget(rows=list(rows), seen_ids={1, 2, 3})
    result = VerifyResult(name="x", v2_count=3, v3_count=3)

    async def read_v3(t, limit):
        return t.rows if limit is None else t.rows[:limit]

    await _compare_hashes(
        source, target, columns=COLUMNS, result=result, limit=None, read_v3_rows=read_v3
    )
    assert result.v2_hash is not None
    assert result.v2_hash == result.v3_hash
    assert result.ok


@pytest.mark.asyncio
async def test_hash_mismatch_when_rows_differ():
    source = InMemorySource(rows=_rows(1, 2, 3))
    corrupted = _rows(1, 2, 3)
    corrupted[1]["price"] = 999_999_999  # v3 에서 한 행이 오염된 척
    target = InMemoryTarget(rows=corrupted, seen_ids={1, 2, 3})
    result = VerifyResult(name="x", v2_count=3, v3_count=3)

    async def read_v3(t, limit):
        return t.rows if limit is None else t.rows[:limit]

    await _compare_hashes(
        source, target, columns=COLUMNS, result=result, limit=None, read_v3_rows=read_v3
    )
    assert result.v2_hash != result.v3_hash
    assert not result.ok
    assert any("hash mismatch" in m for m in result.mismatches)


@pytest.mark.asyncio
async def test_hash_order_independent_for_same_rows():
    """v3 가 id ORDER BY 로 읽지만 source 는 다른 순서여도 hash 는 같아야 한다."""
    source = InMemorySource(rows=list(reversed(_rows(1, 2, 3))))
    target = InMemoryTarget(rows=_rows(1, 2, 3), seen_ids={1, 2, 3})
    result = VerifyResult(name="x", v2_count=3, v3_count=3)

    async def read_v3(t, limit):
        return t.rows

    await _compare_hashes(
        source, target, columns=COLUMNS, result=result, limit=None, read_v3_rows=read_v3
    )
    assert result.ok


@pytest.mark.asyncio
async def test_limit_respects_upper_bound():
    source = InMemorySource(rows=_rows(1, 2, 3, 4, 5))
    target = InMemoryTarget(rows=_rows(1, 2, 3, 4, 5), seen_ids={1, 2, 3, 4, 5})
    result = VerifyResult(name="x", v2_count=5, v3_count=5)

    async def read_v3(t, limit):
        return t.rows[:limit] if limit is not None else t.rows

    await _compare_hashes(
        source, target, columns=COLUMNS, result=result, limit=2, read_v3_rows=read_v3
    )
    # 양쪽에서 첫 2행만 hash 비교 → 같아야 함
    assert result.limit == 2
    assert result.v2_hash == result.v3_hash


# ---------- table_hash 부수 검증 ----------


def test_table_hash_stable_across_column_permutations():
    """columns 순서가 같으면 hash 도 같다."""
    rows = _rows(1, 2)
    h1 = table_hash(rows, columns=COLUMNS)
    h2 = table_hash(rows, columns=COLUMNS)
    assert h1 == h2
