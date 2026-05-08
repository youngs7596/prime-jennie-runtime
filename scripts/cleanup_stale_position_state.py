"""Stale position_state cleanup — KIS 잔고에 없는 ticker 의 sheet 만 close.

dry-run 기본. ``--apply`` 시 실 close. 다른 경우 (zero_qty, kis_drift) 는
처리 안 함 — 수동 검토 대상.

사용:
    docker cp scripts/cleanup_stale_position_state.py <c>:/tmp/cleanup.py
    docker exec <c> python /tmp/cleanup.py            # dry-run
    docker exec <c> python /tmp/cleanup.py --apply    # 실 close
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.config import AppConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cleanup")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="실 close (default dry-run)")
    args = parser.parse_args()
    apply = args.apply

    cfg = AppConfig()
    redis = aioredis.from_url(cfg.redis.url, decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        loaded = await tracker.load_from_redis()
        logger.info("loaded %d position_state entries", loaded)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{cfg.kis.gateway_url}/api/balance")
            resp.raise_for_status()
            balance = resp.json()
        kis_tickers = {p["stock_code"] for p in balance.get("positions", [])}
        logger.info("KIS holdings: %d tickers", len(kis_tickers))

        targets: list[tuple[str, str, int]] = []  # (sheet_id, ticker, qty)
        for sheet_id in tracker.active_sheet_ids():
            st = tracker.get(sheet_id)
            if st is None:
                continue
            if st.ticker not in kis_tickers:
                targets.append((sheet_id, st.ticker, int(st.quantity)))

        logger.info("stale candidates (ticker not in KIS): %d", len(targets))
        for sid, tkr, q in targets:
            logger.info("  %s ticker=%s qty=%d  %s", "CLOSE" if apply else "DRY", tkr, q, sid)

        if apply:
            for sid, _tkr, _q in targets:
                await tracker.close(sid)
            remaining = await tracker.load_from_redis()
            logger.info("closed %d sheets, redis position_state now: %d", len(targets), remaining)
        else:
            logger.info("DRY RUN — pass --apply to actually close")
    finally:
        await redis.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
