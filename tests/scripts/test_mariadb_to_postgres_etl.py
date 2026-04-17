"""ETL 레지스트리/어댑터 단위 테스트.

실 DB 없이 in-memory source + PostgresTarget-모사 로 copy_rows 를 돌려 본다.
MariaDB/asyncpg 의존은 실행 경로에서만 import 되므로 이 모듈은 asyncpg 없어도 로드됨.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from migrations._common import copy_rows
from scripts.mariadb_to_postgres_etl import (
    TABLE_INDEX,
    TABLES,
    MariaDBSource,
    TableSpec,
    _build_where,
    _rows_equal,
)


def test_table_index_covers_all_tables():
    assert {t.v2_table for t in TABLES} == set(TABLE_INDEX)


def test_live_tables_use_same_name_in_v3():
    """운영 라이브 테이블은 v2↔v3 이름 동일 (legacy_* 만 prefix)."""
    legacy_only = {"signal_logs", "trade_logs", "stock_news_sentiments"}
    for t in TABLES:
        if t.v2_table in legacy_only:
            assert t.v3_table.startswith("legacy_"), f"{t.v2_table} should map to legacy_*"
        else:
            assert t.v2_table == t.v3_table, f"{t.v2_table} mismatch {t.v3_table}"


def test_legacy_aliases_match_legacy_namespace_sql():
    """legacy_* 이름은 migrations/002_legacy_namespace.sql / 010 과 정확히 일치."""
    expected = {
        "signal_logs": "legacy_signal_logs",
        "trade_logs": "legacy_trade_logs",
        "stock_news_sentiments": "legacy_news_sentiments",
    }
    for v2, v3 in expected.items():
        assert TABLE_INDEX[v2].v3_table == v3


def test_stock_masters_spec_columns_match_migration_006():
    spec = TABLE_INDEX["stock_masters"]
    assert spec.pk == "stock_code"
    assert spec.columns[0] == "stock_code"
    assert "is_active" in spec.columns
    assert "updated_at" in spec.columns


def test_watchlist_spec_has_composite_pk_and_v2_005_008_columns():
    spec = TABLE_INDEX["watchlist_histories"]
    assert spec.pk == "(snapshot_date, stock_code, run_id)"
    for col in ("quant_score", "sector_group", "market_regime", "run_id", "is_active"):
        assert col in spec.columns


def test_build_where_no_timestamp_col_returns_empty():
    spec = TABLE_INDEX["stock_masters"]  # timestamp_col=None
    clause, params = _build_where(spec, None, None)
    assert clause == ""
    assert params == ()


def test_build_where_with_since_until():
    from datetime import datetime

    spec = TABLE_INDEX["trade_logs"]  # timestamp_col='trade_timestamp'
    clause, params = _build_where(
        spec,
        datetime(2026, 1, 1),
        datetime(2026, 2, 1),
    )
    assert "trade_timestamp >= %s" in clause
    assert "trade_timestamp < %s" in clause
    assert len(params) == 2


def test_rows_equal_none_equivalence():
    a = {"x": None, "y": 1}
    b = {"x": None, "y": 1}
    assert _rows_equal(a, b)


def test_rows_equal_date_isoformat_tolerant():
    from datetime import datetime

    a = {"ts": datetime(2026, 4, 1, 9, 0, 0)}
    b = {"ts": "2026-04-01 09:00:00"}
    # str(datetime) == "2026-04-01 09:00:00" (ISO + space) — 동등.
    assert _rows_equal(a, b)


def test_rows_equal_mismatch():
    a = {"x": 1}
    b = {"x": 2}
    assert not _rows_equal(a, b)


# ─────────────────────────────────────────────────────────────────────
# copy_rows + MariaDBSource 와 동등한 in-memory driver 로 streaming 확인.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class FakeCursor:
    rows: list[tuple]
    _pos: int = 0

    async def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._pos = 0

    async def fetchone(self) -> tuple:
        return (len(self.rows),)

    async def fetchmany(self, size: int) -> list[tuple]:
        chunk = self.rows[self._pos : self._pos + size]
        self._pos += size
        return chunk

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@dataclass
class FakeV2Conn:
    rows: list[tuple]

    def cursor(self) -> FakeCursor:
        return FakeCursor(rows=self.rows)


@dataclass
class FakeV3Target:
    seen_pk: set[Any] = field(default_factory=set)
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def write_batch(self, batch: list[dict[str, Any]]) -> int:
        copied = 0
        for r in batch:
            key = tuple(r[c] for c in sorted(r.keys()) if c in ("id", "stock_code"))
            if key in self.seen_pk:
                continue
            self.seen_pk.add(key)
            self.rows.append(dict(r))
            copied += 1
        return copied

    async def count(self) -> int:
        return len(self.rows)


@pytest.mark.asyncio
async def test_mariadb_source_streaming_yields_dict_rows():
    spec = TableSpec(
        v2_table="t",
        v3_table="t",
        columns=("id", "x"),
        pk="id",
        sample_order_by="id",
    )
    v2 = FakeV2Conn(rows=[(1, "a"), (2, "b"), (3, "c")])
    src = MariaDBSource(conn=v2, spec=spec)
    target = FakeV3Target()
    stats = await copy_rows(src, target, batch_size=2)
    assert stats.source_total == 3
    assert stats.copied == 3
    assert target.rows[0] == {"id": 1, "x": "a"}


@pytest.mark.asyncio
async def test_mariadb_source_empty_ok():
    spec = TableSpec(v2_table="t", v3_table="t", columns=("id",), pk="id")
    src = MariaDBSource(conn=FakeV2Conn(rows=[]), spec=spec)
    target = FakeV3Target()
    stats = await copy_rows(src, target, batch_size=10)
    assert stats.source_total == 0
    assert stats.copied == 0
