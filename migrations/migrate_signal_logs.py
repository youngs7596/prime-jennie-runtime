"""v2 signal_logs → v3 legacy_signal_logs 마이그레이션.

사용:

    python -m migrations.migrate_signal_logs \\
        --batch-size 1000 \\
        --since 2026-01-01 --until 2026-04-17 \\
        --dry-run

v2 컬럼 (17개) 전부 1:1 복사. PK(id) 충돌 시 skip (idempotent).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ._common import (
    LegacyRowSource,
    MigrationStats,
    TargetWriter,
    build_insert_sql,
    copy_rows,
)

logger = logging.getLogger(__name__)

TABLE_V2 = "signal_logs"
TABLE_V3 = "legacy_signal_logs"

COLUMNS = [
    "id",
    "signal_type",
    "stock_code",
    "stock_name",
    "strategy",
    "price",
    "quantity",
    "hybrid_score",
    "rsi_value",
    "volume_ratio",
    "market_regime",
    "position_multiplier",
    "profit_pct",
    "holding_days",
    "status",
    "suppressed_reason",
    "created_at",
]

SELECT_SQL = f"SELECT {', '.join(COLUMNS)} FROM {TABLE_V2}"
INSERT_SQL = build_insert_sql(table=TABLE_V3, columns=COLUMNS, on_conflict_pk="id")


# ---------------------------------------------------------------------
# MariaDB source (aiomysql) — 지연 import
# ---------------------------------------------------------------------


@dataclass
class MariaDBSignalSource:
    """aiomysql 기반 v2 source. 실 실행 시 연결 객체 주입.

    ``conn``: ``aiomysql.Connection`` (autocommit 여부 무관 — SELECT only).
    """

    conn: Any  # aiomysql.Connection (런타임 타입)

    async def count(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        where, params = _build_where(since, until, timestamp_col="created_at")
        sql = f"SELECT COUNT(*) FROM {TABLE_V2}{where}"
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            (n,) = await cur.fetchone()
            return int(n)

    async def iter_rows(
        self,
        *,
        batch_size: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        where, params = _build_where(since, until, timestamp_col="created_at")
        sql = f"{SELECT_SQL}{where} ORDER BY id"
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            while True:
                rows = await cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(zip(COLUMNS, row, strict=True))


# ---------------------------------------------------------------------
# Postgres target (asyncpg)
# ---------------------------------------------------------------------


@dataclass
class PostgresSignalTarget:
    """asyncpg 기반 v3 target. ``conn``: ``asyncpg.Connection``."""

    conn: Any  # asyncpg.Connection

    async def write_batch(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        tuples = [tuple(r.get(c) for c in COLUMNS) for r in rows]
        # asyncpg 는 ON CONFLICT 건수를 반환하지 않으므로 전/후 count 차이로 계산하는 대신
        # ``executemany`` 후 우리 batch size 를 그대로 copied 로 간주하고, skipped 는 0 으로 둔다.
        # (정확한 skipped 는 verify_migration 이 v2/v3 count 차로 감지)
        await self.conn.executemany(INSERT_SQL, tuples)
        return len(tuples)

    async def count(self) -> int:
        (n,) = await self.conn.fetchrow(f"SELECT COUNT(*) FROM {TABLE_V3}")
        return int(n)


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------


async def run_with_sources(
    source: LegacyRowSource,
    target: TargetWriter,
    *,
    batch_size: int = 1000,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
) -> MigrationStats:
    """Source/Target 주입형 실행 — 테스트용 + CLI 공통 진입점."""
    return await copy_rows(
        source,
        target,
        batch_size=batch_size,
        since=since,
        until=until,
        dry_run=dry_run,
    )


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # 지연 import — 드라이버 미설치 환경에서도 모듈 import 는 가능.
    import aiomysql  # type: ignore[import-not-found]
    import asyncpg  # type: ignore[import-not-found]

    v2_conn = await aiomysql.connect(
        host=args.v2_host,
        port=args.v2_port,
        user=args.v2_user,
        password=args.v2_password,
        db=args.v2_db,
        charset="utf8mb4",
    )
    v3_conn = await asyncpg.connect(args.v3_dsn)

    try:
        source = MariaDBSignalSource(conn=v2_conn)
        target = PostgresSignalTarget(conn=v3_conn)
        stats = await run_with_sources(
            source,
            target,
            batch_size=args.batch_size,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
        )
    finally:
        v2_conn.close()
        await v3_conn.close()

    logger.info(
        "migration done: source=%d copied=%d skipped=%d batches=%d errors=%d",
        stats.source_total,
        stats.copied,
        stats.skipped,
        stats.batches,
        len(stats.errors),
    )
    return 0 if stats.ok else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Migrate v2 {TABLE_V2} → v3 {TABLE_V3}")
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--since", type=_parse_datetime, default=None)
    p.add_argument("--until", type=_parse_datetime, default=None)
    p.add_argument("--dry-run", action="store_true")
    # v2 MariaDB
    p.add_argument("--v2-host", default="localhost")
    p.add_argument("--v2-port", type=int, default=3307)
    p.add_argument("--v2-user", default="prime")
    p.add_argument("--v2-password", default="")
    p.add_argument("--v2-db", default="prime_jennie")
    # v3 Postgres (DSN 한 줄)
    p.add_argument(
        "--v3-dsn",
        default="postgresql://pj_runtime:dev_password@localhost:5432/prime_jennie_v3",
    )
    return p.parse_args(argv)


def _parse_datetime(s: str) -> datetime:
    """YYYY-MM-DD 또는 ISO8601."""
    if len(s) == 10:
        return datetime.fromisoformat(s + "T00:00:00")
    return datetime.fromisoformat(s)


def _build_where(
    since: datetime | None,
    until: datetime | None,
    *,
    timestamp_col: str,
) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append(f"{timestamp_col} >= %s")
        params.append(since)
    if until is not None:
        clauses.append(f"{timestamp_col} < %s")
        params.append(until)
    if not clauses:
        return "", ()
    return " WHERE " + " AND ".join(clauses), tuple(params)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
