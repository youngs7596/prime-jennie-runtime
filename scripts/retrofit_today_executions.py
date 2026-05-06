"""오늘 (2026-05-06) fast-loop 매매 backfill 스크립트.

prime-jennie-runtime 의 entry_executor 가 PG persist 코드 결락 상태로 운영되어,
오늘 KIS 에서 체결된 49건 매수가 ``executions`` / ``positions`` 에 한 건도
적히지 않았다. 이 스크립트는:

1. Redis ``position_state:*`` 의 sheet 별 상태를 읽어 ``executions`` 에
   side='buy' 로 INSERT (이미 있는 sheet_id 는 skip).
2. KIS Gateway ``GET /api/balance`` 의 실 잔고 45 종목을 ground truth 로 삼아
   ``positions`` 테이블을 재구성 (기존 stale row 삭제 후 INSERT).

사용:
    uv run python -m scripts.retrofit_today_executions --dry-run
    uv run python -m scripts.retrofit_today_executions --apply

``--dry-run`` 은 변경 예정 row 만 출력하고 commit 하지 않음. ``--apply`` 가 실 변경.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.config import AppConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("retrofit")


async def fetch_kis_balance(gateway_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{gateway_url}/api/balance")
        resp.raise_for_status()
        return resp.json()


async def collect_redis_states(redis: aioredis.Redis) -> dict[str, Any]:
    """position_state:* 키를 in-memory 로 로드 (PositionTracker.load_from_redis 활용)."""
    tracker = PositionTracker(redis)
    count = await tracker.load_from_redis()
    logger.info("loaded %d position_state entries from redis", count)
    states = {}
    for sheet_id in tracker.active_sheet_ids():
        state = tracker.get(sheet_id)
        if state is not None:
            states[sheet_id] = state
    return states


async def backfill_executions(
    pool: asyncpg.Pool,
    states: dict[str, Any],
    *,
    apply: bool,
) -> int:
    """state 별로 executions 에 side='buy' INSERT — 이미 있는 sheet_id 는 skip."""
    inserted = 0
    skipped = 0
    async with pool.acquire() as conn:
        existing = {
            r["sheet_id"]
            for r in await conn.fetch("SELECT DISTINCT sheet_id FROM executions WHERE side = 'buy'")
        }
        async with conn.transaction():
            for sheet_id, state in states.items():
                if sheet_id in existing:
                    skipped += 1
                    continue
                metadata = {
                    "backfilled": True,
                    "source": "retrofit_today_executions",
                }
                if apply:
                    await conn.execute(
                        """
                        INSERT INTO executions
                            (sheet_id, side, price, qty, executed_at,
                             slippage_bps, metadata_json)
                        VALUES ($1, 'buy', $2, $3, $4, NULL, $5)
                        """,
                        sheet_id,
                        state.entry_price,
                        state.quantity,
                        state.entered_at,
                        json.dumps(metadata),
                    )
                logger.info(
                    "%s execution: sheet=%s ticker=%s price=%.0f qty=%d",
                    "INSERT" if apply else "DRY",
                    sheet_id,
                    state.ticker,
                    state.entry_price,
                    state.quantity,
                )
                inserted += 1
            if not apply:
                # dry-run 은 트랜잭션 롤백
                raise _DryRunRollbackError()
    logger.info("backfill_executions: inserted=%d skipped=%d", inserted, skipped)
    return inserted


class _DryRunRollbackError(Exception):
    pass


async def sync_positions_from_kis(
    pool: asyncpg.Pool,
    balance: dict[str, Any],
    *,
    apply: bool,
) -> tuple[int, int]:
    """KIS 잔고 ground truth 로 positions 재구성. (deleted, inserted)."""
    kis_positions = balance.get("positions", [])
    kis_codes = {p["stock_code"] for p in kis_positions}

    async with pool.acquire() as conn:
        existing = {
            r["stock_code"]: r for r in await conn.fetch("SELECT stock_code FROM positions")
        }
        existing_codes = set(existing.keys())

        to_delete = existing_codes - kis_codes
        # KIS 에 있는 종목 — 평단/수량을 KIS 값으로 UPSERT (기존 row 가 있어도 덮어씀)
        try:
            async with conn.transaction():
                # 1. KIS 잔고에 없는 stale row 삭제
                for code in to_delete:
                    if apply:
                        await conn.execute("DELETE FROM positions WHERE stock_code = $1", code)
                    logger.info(
                        "%s position: stock_code=%s (stale, not in KIS)",
                        "DELETE" if apply else "DRY DEL",
                        code,
                    )

                # 2. KIS 잔고 종목들 UPSERT
                inserted = 0
                for p in kis_positions:
                    metadata_qty = int(p["quantity"])
                    metadata_avg = int(p["average_buy_price"])
                    metadata_total = int(p.get("total_buy_amount") or metadata_qty * metadata_avg)
                    if apply:
                        await conn.execute(
                            """
                            INSERT INTO positions
                                (stock_code, stock_name, quantity, average_buy_price,
                                 total_buy_amount, created_at, updated_at)
                            VALUES ($1, $2, $3, $4, $5, now(), now())
                            ON CONFLICT (stock_code) DO UPDATE SET
                                stock_name = EXCLUDED.stock_name,
                                quantity = EXCLUDED.quantity,
                                average_buy_price = EXCLUDED.average_buy_price,
                                total_buy_amount = EXCLUDED.total_buy_amount,
                                updated_at = now()
                            """,
                            p["stock_code"],
                            p["stock_name"],
                            metadata_qty,
                            metadata_avg,
                            metadata_total,
                        )
                    logger.info(
                        "%s position: %s %s qty=%d avg=%d",
                        "UPSERT" if apply else "DRY UP",
                        p["stock_code"],
                        p["stock_name"],
                        metadata_qty,
                        metadata_avg,
                    )
                    inserted += 1

                if not apply:
                    raise _DryRunRollbackError()
        except _DryRunRollbackError:
            pass

    return len(to_delete), inserted


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="실 변경 적용")
    parser.add_argument("--dry-run", action="store_true", help="변경 예정만 출력 (default)")
    args = parser.parse_args(argv)
    apply = args.apply
    if not apply:
        logger.info("DRY RUN — pass --apply to commit changes")

    cfg = AppConfig()
    redis = aioredis.from_url(cfg.redis.url, decode_responses=False)
    pool = await asyncpg.create_pool(
        host=cfg.postgres.host,
        port=cfg.postgres.port,
        user=cfg.postgres.user,
        password=cfg.postgres.password,
        database=cfg.postgres.db,
        min_size=1,
        max_size=2,
    )
    try:
        # 1. KIS 잔고 조회
        balance = await fetch_kis_balance(cfg.kis.gateway_url)
        logger.info(
            "KIS balance: %d positions, cash=%d, total=%d",
            balance.get("position_count", 0),
            balance.get("cash_balance", 0),
            balance.get("total_asset", 0),
        )

        # 2. Redis state 수집
        states = await collect_redis_states(redis)

        # 3. executions 백필
        try:
            n_inserted = await backfill_executions(pool, states, apply=apply)
        except _DryRunRollbackError:
            n_inserted = -1

        # 4. positions sync
        n_deleted, n_upserted = await sync_positions_from_kis(pool, balance, apply=apply)

        logger.info(
            "RESULT: executions inserted=%d, positions deleted=%d / upserted=%d",
            n_inserted,
            n_deleted,
            n_upserted,
        )
    finally:
        await pool.close()
        await redis.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
