"""PositionTracker 진단 — Redis position_state:* vs KIS 잔고 비교.

dry-run 전용 (변경 없음). 사용:
    docker cp scripts/inspect_position_tracker.py <c>:/tmp/inspect.py
    docker exec <c> python /tmp/inspect.py
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.config import AppConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("inspect")


async def main() -> None:
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
        kis_qty: dict[str, int] = {
            p["stock_code"]: int(p["quantity"]) for p in balance.get("positions", [])
        }
        logger.info("KIS holdings: %d tickers", len(kis_qty))

        sheets_by_ticker: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
        for sheet_id in tracker.active_sheet_ids():
            st = tracker.get(sheet_id)
            if st is None:
                continue
            sheets_by_ticker[st.ticker].append((sheet_id, int(st.quantity), st.entry_price))

        zero_qty: list[tuple[str, str]] = []
        not_in_kis: list[tuple[str, str, int]] = []
        kis_aligned: list[tuple[str, str, int, int, int]] = []  # ticker, sheet, sheet_qty, kis_qty, n_sheets
        kis_drift: list[tuple[str, int, int, int]] = []  # ticker, sum_sheet_qty, kis_qty, n_sheets

        for ticker, sheets in sorted(sheets_by_ticker.items()):
            sum_qty = sum(q for _, q, _ in sheets)
            kqty = kis_qty.get(ticker, 0)
            for sid, q, _ep in sheets:
                if q == 0:
                    zero_qty.append((ticker, sid))
                elif kqty == 0:
                    not_in_kis.append((ticker, sid, q))
            if kqty > 0:
                if sum_qty == kqty:
                    for sid, q, _ep in sheets:
                        if q > 0:
                            kis_aligned.append((ticker, sid, q, kqty, len(sheets)))
                else:
                    kis_drift.append((ticker, sum_qty, kqty, len(sheets)))

        print("\n=== zero_qty sheets (state.quantity == 0, safe to close) ===")
        for ticker, sid in zero_qty:
            print(f"  {ticker}  {sid}")
        print(f"  total: {len(zero_qty)}")

        print("\n=== not_in_kis sheets (ticker not in KIS, safe to close) ===")
        for ticker, sid, q in not_in_kis:
            print(f"  {ticker}  qty={q}  {sid}")
        print(f"  total: {len(not_in_kis)}")

        print("\n=== kis_aligned (sum_sheet_qty == kis_qty, KEEP) ===")
        seen_ticker = set()
        for ticker, _sid, _q, _kqty, _n in kis_aligned:
            if ticker not in seen_ticker:
                seen_ticker.add(ticker)
                ticker_sheets = [s for s in kis_aligned if s[0] == ticker]
                print(
                    f"  {ticker} kis={ticker_sheets[0][3]}  sheets={ticker_sheets[0][4]}  "
                    f"sum_sheet_qty={sum(s[2] for s in ticker_sheets)}"
                )
        print(f"  unique tickers: {len(seen_ticker)}, total sheets: {len(kis_aligned)}")

        print("\n=== kis_drift (sum_sheet_qty != kis_qty — manual decision) ===")
        for ticker, sum_q, kqty, n in kis_drift:
            print(f"  {ticker}  sheet_sum={sum_q}  kis={kqty}  sheets={n}")
        print(f"  total tickers: {len(kis_drift)}")

        print("\n=== KIS tickers with NO sheets (orphan KIS holdings) ===")
        kis_no_sheet = [t for t in kis_qty if t not in sheets_by_ticker]
        for ticker in kis_no_sheet:
            print(f"  {ticker}  kis_qty={kis_qty[ticker]}")
        print(f"  total: {len(kis_no_sheet)}")

        print("\n=== summary ===")
        print(f"  redis position_state count: {loaded}")
        print(f"  zero_qty (close): {len(zero_qty)}")
        print(f"  not_in_kis (close): {len(not_in_kis)}")
        print(f"  kis_aligned (keep): {len(kis_aligned)}")
        print(f"  kis_drift sheets (review): {sum(n for _, _, _, n in kis_drift)}")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
