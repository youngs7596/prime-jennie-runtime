"""v2 trade_logs → v3 legacy_trade_logs 마이그레이션.

사용:

    python -m migrations.migrate_trade_logs \\
        --batch-size 1000 \\
        --since 2026-01-01 --until 2026-04-17

v2 컬럼 (17개) 전부 1:1 복사. PK(id) 충돌 시 skip.
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

TABLE_V2 = "trade_logs"
TABLE_V3 = "legacy_trade_logs"

COLUMNS = [
    "id",
    "stock_code",
    "stock_name",
    "trade_type",
    "quantity",
    "price",
    "total_amount",
    "reason",
    "strategy_signal",
    "market_regime",
    "llm_score",
    "hybrid_score",
    "trade_tier",
    "profit_pct",
    "profit_amount",
    "holding_days",
    "trade_timestamp",
]

SELECT_SQL = f"SELECT {', '.join(COLUMNS)} FROM {TABLE_V2}"
INSERT_SQL = build_insert_sql(table=TABLE_V3, columns=COLUMNS, on_conflict_pk="id")


@dataclass
class MariaDBTradeSource:
    conn: Any  # aiomysql.Connection

    async def count(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        where, params = _build_where(since, until, timestamp_col="trade_timestamp")
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
        where, params = _build_where(since, until, timestamp_col="trade_timestamp")
        sql = f"{SELECT_SQL}{where} ORDER BY id"
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            while True:
                rows = await cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(zip(COLUMNS, row, strict=True))


@dataclass
class PostgresTradeTarget:
    conn: Any  # asyncpg.Connection

    async def write_batch(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        tuples = [tuple(r.get(c) for c in COLUMNS) for r in rows]
        await self.conn.executemany(INSERT_SQL, tuples)
        return len(tuples)

    async def count(self) -> int:
        (n,) = await self.conn.fetchrow(f"SELECT COUNT(*) FROM {TABLE_V3}")
        return int(n)


async def run_with_sources(
    source: LegacyRowSource,
    target: TargetWriter,
    *,
    batch_size: int = 1000,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
) -> MigrationStats:
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
        source = MariaDBTradeSource(conn=v2_conn)
        target = PostgresTradeTarget(conn=v3_conn)
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
    p.add_argument("--v2-host", default="localhost")
    p.add_argument("--v2-port", type=int, default=3307)
    p.add_argument("--v2-user", default="prime")
    p.add_argument("--v2-password", default="")
    p.add_argument("--v2-db", default="prime_jennie")
    p.add_argument(
        "--v3-dsn",
        default="postgresql://pj_runtime:dev_password@localhost:5432/prime_jennie_v3",
    )
    return p.parse_args(argv)


def _parse_datetime(s: str) -> datetime:
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
