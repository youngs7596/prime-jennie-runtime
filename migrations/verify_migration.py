"""v2 MariaDB ↔ v3 Postgres legacy_* 테이블 무결성 검증.

두 단계:
- **count check**: 두 쪽 COUNT(*) 가 같은지
- **hash check**: 같은 컬럼 셋으로 ``table_hash`` 를 산출해 일치하는지
  (대용량에는 ``--limit`` 로 id 범위 샘플링)

사용:

    python -m migrations.verify_migration signal_logs --v2 ... --v3 ...
    python -m migrations.verify_migration trade_logs --limit 10000
    python -m migrations.verify_migration quant_scores

Phase 1 skeleton: 실 드라이버는 지연 import. 핵심 비교 로직 (``_compare_counts``,
``_compare_hashes``) 은 단위 테스트에서 in-memory source/target 으로 커버.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import migrate_daily_quant_scores as quant
from . import migrate_signal_logs as signal
from . import migrate_trade_logs as trade
from ._common import LegacyRowSource, TargetWriter, table_hash

# ``read_v3_rows`` 콜백 타입. Protocol 인 TargetWriter 가 '읽기' 는 모르므로 별도 주입.
_ReadRowsFn = Callable[[TargetWriter, int | None], Awaitable[list[dict[str, Any]]]]

logger = logging.getLogger(__name__)

# 각 마이그레이션별 구성 요약
_SPECS = {
    "signal_logs": {
        "v2_table": signal.TABLE_V2,
        "v3_table": signal.TABLE_V3,
        "columns": signal.COLUMNS,
    },
    "trade_logs": {
        "v2_table": trade.TABLE_V2,
        "v3_table": trade.TABLE_V3,
        "columns": trade.COLUMNS,
    },
    "quant_scores": {
        "v2_table": quant.TABLE_V2,
        "v3_table": quant.TABLE_V3,
        "columns": quant.COLUMNS,
    },
}


@dataclass
class VerifyResult:
    name: str
    v2_count: int
    v3_count: int
    v2_hash: str | None = None
    v3_hash: str | None = None
    limit: int | None = None
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


# ---------------------------------------------------------------------
# 핵심 비교 로직 — Protocol 기반 (테스트 가능)
# ---------------------------------------------------------------------


async def _compare_counts(
    source: LegacyRowSource, target: TargetWriter, *, name: str
) -> VerifyResult:
    v2 = await source.count()
    v3 = await target.count()
    result = VerifyResult(name=name, v2_count=v2, v3_count=v3)
    if v2 != v3:
        result.mismatches.append(f"count mismatch: v2={v2} v3={v3}")
    return result


async def _compare_hashes(
    source: LegacyRowSource,
    target: TargetWriter,
    *,
    columns: list[str],
    result: VerifyResult,
    limit: int | None,
    read_v3_rows: _ReadRowsFn,
) -> None:
    """v2/v3 양쪽에서 `limit` 이하의 행을 읽어 ``table_hash`` 비교.

    ``read_v3_rows`` 는 target 에서 행을 읽어 dict 리스트로 돌려주는 함수.
    Protocol 인 TargetWriter 가 읽기를 모르므로 별도 주입.
    """
    result.limit = limit

    v2_rows: list[dict[str, Any]] = []
    async for row in source.iter_rows(batch_size=min(limit or 1000, 1000)):
        v2_rows.append(row)
        if limit is not None and len(v2_rows) >= limit:
            break

    v3_rows = await read_v3_rows(target, limit=limit)

    result.v2_hash = table_hash(v2_rows, columns=columns)
    result.v3_hash = table_hash(v3_rows, columns=columns)
    if result.v2_hash != result.v3_hash:
        result.mismatches.append(
            f"hash mismatch: v2={result.v2_hash[:12]}… v3={result.v3_hash[:12]}…"
        )


# ---------------------------------------------------------------------
# 실 드라이버 실행 (지연 import)
# ---------------------------------------------------------------------


async def _run_verify(
    name: str,
    *,
    v2_conn: Any,
    v3_conn: Any,
    limit: int | None,
) -> VerifyResult:
    spec = _SPECS[name]
    source = _build_v2_source(name, v2_conn)
    target = _build_v3_target(name, v3_conn)

    result = await _compare_counts(source, target, name=name)
    await _compare_hashes(
        source,
        target,
        columns=spec["columns"],
        result=result,
        limit=limit,
        read_v3_rows=_read_v3_rows_factory(name),
    )
    return result


def _build_v2_source(name: str, conn: Any) -> LegacyRowSource:
    if name == "signal_logs":
        return signal.MariaDBSignalSource(conn=conn)
    if name == "trade_logs":
        return trade.MariaDBTradeSource(conn=conn)
    if name == "quant_scores":
        return quant.MariaDBQuantSource(conn=conn)
    raise ValueError(f"unknown migration name: {name}")


def _build_v3_target(name: str, conn: Any) -> TargetWriter:
    if name == "signal_logs":
        return signal.PostgresSignalTarget(conn=conn)
    if name == "trade_logs":
        return trade.PostgresTradeTarget(conn=conn)
    if name == "quant_scores":
        return quant.PostgresQuantTarget(conn=conn)
    raise ValueError(f"unknown migration name: {name}")


def _read_v3_rows_factory(name: str) -> _ReadRowsFn:
    """v3(asyncpg) 에서 rows 를 읽는 함수. target.conn 직접 사용."""
    spec = _SPECS[name]
    columns = spec["columns"]
    table = spec["v3_table"]

    async def _read(target: TargetWriter, limit: int | None) -> list[dict[str, Any]]:
        col_list = ", ".join(columns)
        sql = f"SELECT {col_list} FROM {table} ORDER BY id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        records = await target.conn.fetch(sql)  # type: ignore[attr-defined]
        return [dict(r) for r in records]

    return _read


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


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
        result = await _run_verify(
            args.name,
            v2_conn=v2_conn,
            v3_conn=v3_conn,
            limit=args.limit,
        )
    finally:
        v2_conn.close()
        await v3_conn.close()

    _print_result(result)
    return 0 if result.ok else 2


def _print_result(result: VerifyResult) -> None:
    logger.info("verify %s: v2_count=%d v3_count=%d", result.name, result.v2_count, result.v3_count)
    if result.v2_hash and result.v3_hash:
        logger.info("  hash v2=%s v3=%s", result.v2_hash[:16], result.v3_hash[:16])
    if result.ok:
        logger.info("  OK")
    else:
        for m in result.mismatches:
            logger.error("  MISMATCH: %s", m)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify v2 ↔ v3 legacy_* migration")
    p.add_argument("name", choices=sorted(_SPECS.keys()))
    p.add_argument("--limit", type=int, default=None, help="샘플 비교용 (대용량 시)")
    # v2 MariaDB
    p.add_argument("--v2-host", default="localhost")
    p.add_argument("--v2-port", type=int, default=3307)
    p.add_argument("--v2-user", default="prime")
    p.add_argument("--v2-password", default="")
    p.add_argument("--v2-db", default="prime_jennie")
    # v3 Postgres
    p.add_argument(
        "--v3-dsn",
        default="postgresql://pj_runtime:dev_password@localhost:5432/prime_jennie_v3",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
