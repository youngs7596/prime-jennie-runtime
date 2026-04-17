"""v2 → v3 마이그레이션 공통 헬퍼.

skeleton. 실 MariaDB/Postgres 접속은 영석 직접 — 아래 Protocol 을 실 드라이버
(aiomysql / asyncpg) 어댑터로 구현하면 CLI 가 동작한다.

핵심 결정:
- **스키마 변환 없음**: v2 컬럼을 legacy_* 테이블로 1:1 복사. 재해석은 meta Eval 쪽에서.
- **idempotent upsert**: PK(또는 unique) 충돌 시 skip. 스크립트 재실행이 안전.
- **결정론 hash**: verify_migration 이 v2/v3 양쪽 `table_hash` 비교로 무결성 확인.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class LegacyRowSource(Protocol):
    """v2 쪽 소스 (MariaDB). 한 번에 batch_size 개씩 yield."""

    async def iter_rows(
        self,
        *,
        batch_size: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def count(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int: ...


@runtime_checkable
class TargetWriter(Protocol):
    """v3 쪽 타겟 (Postgres). 배치 단위로 upsert."""

    async def write_batch(self, rows: list[dict[str, Any]]) -> int:
        """성공 insert 개수 반환 (중복 skip 제외)."""
        ...

    async def count(self) -> int: ...


@dataclass
class MigrationStats:
    """한 번의 마이그레이션 실행 요약."""

    source_total: int = 0
    copied: int = 0
    skipped: int = 0
    batches: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def copy_rows(
    source: LegacyRowSource,
    target: TargetWriter,
    *,
    batch_size: int = 1000,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
    progress_every: int = 10,
) -> MigrationStats:
    """Source → Target 복사 주 함수.

    read/write/stats 에만 집중. 스키마 변환은 Protocol 구현체 내부에서.
    `dry_run=True` 면 read 만 수행하고 write 를 스킵 (source 쿼리 검증용).
    """
    stats = MigrationStats()
    stats.source_total = await source.count(since=since, until=until)

    batch: list[dict[str, Any]] = []
    async for row in source.iter_rows(batch_size=batch_size, since=since, until=until):
        batch.append(row)
        if len(batch) >= batch_size:
            await _flush(batch, target, stats, dry_run=dry_run)
            if stats.batches % progress_every == 0:
                logger.info(
                    "batch %d: copied=%d skipped=%d", stats.batches, stats.copied, stats.skipped
                )
            batch = []

    if batch:
        await _flush(batch, target, stats, dry_run=dry_run)

    return stats


async def _flush(
    batch: list[dict[str, Any]],
    target: TargetWriter,
    stats: MigrationStats,
    *,
    dry_run: bool,
) -> None:
    stats.batches += 1
    if dry_run:
        return
    try:
        copied = await target.write_batch(batch)
        stats.copied += copied
        stats.skipped += len(batch) - copied
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"batch {stats.batches}: {type(e).__name__}: {e}")


def row_hash(row: dict[str, Any], *, columns: list[str]) -> str:
    """행 하나의 결정론적 sha256 해시.

    datetime/date → isoformat. None → "". bytes → hex. 그 외 json.dumps default=str.
    verify_migration 이 v2/v3 hash set 비교에 쓴다.
    """
    payload: dict[str, Any] = {}
    for k in columns:
        v = row.get(k)
        if v is None:
            payload[k] = ""
        elif isinstance(v, datetime | date):
            payload[k] = v.isoformat()
        elif isinstance(v, bytes | bytearray):
            payload[k] = v.hex()
        else:
            payload[k] = v
    serialized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


def table_hash(rows: list[dict[str, Any]], *, columns: list[str]) -> str:
    """테이블 전체의 결정론적 집계 해시. 행 순서 무관.

    각 행의 row_hash 를 정렬 후 concat 하여 sha256.
    """
    hashes = sorted(row_hash(r, columns=columns) for r in rows)
    joined = "\n".join(hashes)
    return hashlib.sha256(joined.encode()).hexdigest()


def build_insert_sql(
    *,
    table: str,
    columns: list[str],
    on_conflict_pk: str = "id",
) -> str:
    """asyncpg 용 배치 INSERT ... ON CONFLICT DO NOTHING 생성.

    $1, $2, ... 순서는 `columns` 순서와 일치. 호출자는 rows 를 columns 순서 튜플로
    직렬화해서 executemany 에 건네면 된다.
    """
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    col_list = ", ".join(columns)
    pk_clause = on_conflict_pk if on_conflict_pk.startswith("(") else f"({on_conflict_pk})"
    return (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT {pk_clause} DO NOTHING"
    )


__all__ = [
    "LegacyRowSource",
    "MigrationStats",
    "TargetWriter",
    "build_insert_sql",
    "copy_rows",
    "row_hash",
    "table_hash",
]
