"""테스트용 in-memory source/target (실 aiomysql/asyncpg 없이도 copy_rows 검증)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InMemorySource:
    """LegacyRowSource 구현. 시간 컬럼 기준 since/until 필터."""

    rows: list[dict[str, Any]]
    timestamp_col: str = "created_at"

    async def count(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return len(self._filtered(since, until))

    async def iter_rows(
        self,
        *,
        batch_size: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        for row in self._filtered(since, until):
            yield dict(row)

    def _filtered(
        self,
        since: datetime | None,
        until: datetime | None,
    ) -> list[dict[str, Any]]:
        out = []
        for r in self.rows:
            ts = r.get(self.timestamp_col)
            if since is not None and ts is not None and ts < since:
                continue
            if until is not None and ts is not None and ts >= until:
                continue
            out.append(r)
        return out


@dataclass
class InMemoryTarget:
    """TargetWriter 구현. id 기준 de-dup 으로 idempotent upsert 모사."""

    seen_ids: set[int] = field(default_factory=set)
    rows: list[dict[str, Any]] = field(default_factory=list)
    fail_on_batch: int | None = None  # 해당 batch index 에서 예외 발생 (1-based)
    _batch_idx: int = 0

    async def write_batch(self, batch: list[dict[str, Any]]) -> int:
        self._batch_idx += 1
        if self.fail_on_batch is not None and self._batch_idx == self.fail_on_batch:
            raise RuntimeError("synthetic write failure")
        copied = 0
        for row in batch:
            rid = row.get("id")
            if rid in self.seen_ids:
                continue
            self.seen_ids.add(rid)
            self.rows.append(dict(row))
            copied += 1
        return copied

    async def count(self) -> int:
        return len(self.rows)
