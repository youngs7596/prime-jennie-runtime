"""LivePositionsPoller — KIS Gateway `/balance` 주기 polling → Redis 스냅샷.

v2 원본: `prime_jennie/services/monitor/app.py` `_publish_live_snapshot` + tick consumer.
v3 는 fast_loop/tick_loop 이 실시간 매도 판정을 담당하므로, monitor 는 대시보드용
스냅샷 + metrics 에 집중.

Redis 키 (v2 와 동일):
- `monitoring:live_positions` — JSON {positions: [...], updated_at: iso}
- `monitoring:price_monitor`  — JSON {status, watching_count, updated_at}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

LIVE_POSITIONS_KEY = "monitoring:live_positions"
MONITOR_STATUS_KEY = "monitoring:price_monitor"


class LivePositionsPoller:
    """주기적으로 KIS Gateway `/balance` 를 읽어 Redis 에 스냅샷을 쓴다.

    Parameters
    ----------
    redis_client:
        async redis 클라이언트.
    gateway_url:
        KIS Gateway base URL (예: `http://kis-gateway:8080`).
    interval_sec:
        polling 주기.
    client:
        httpx AsyncClient — 테스트에서 주입. None 이면 내부에서 생성.
    """

    def __init__(
        self,
        *,
        redis_client: aioredis.Redis,
        gateway_url: str,
        interval_sec: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._redis = redis_client
        self._gateway_url = gateway_url.rstrip("/")
        self._interval = interval_sec
        self._client = client
        self._owned_client = client is None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # 최근 성공/실패 타임스탬프 — metrics 에서 노출.
        self.last_success_ts: float | None = None
        self.last_failure_ts: float | None = None
        self.last_positions_count: int = 0

    async def __aenter__(self) -> LivePositionsPoller:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="live-positions-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._interval + 2.0)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("live positions poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ one tick

    async def tick_once(self) -> int:
        """1회 polling. 성공 시 positions 개수를 반환, 실패 시 0."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
            self._owned_client = True
        try:
            resp = await self._client.get(f"{self._gateway_url}/balance")
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            self.last_failure_ts = time.time()
            logger.warning("balance fetch failed", exc_info=True)
            return 0

        positions = payload.get("positions", []) or []
        snapshot = {
            "positions": positions,
            "updated_at": datetime.now(UTC).isoformat(),
            "cash_balance": payload.get("cash_balance"),
            "total_asset": payload.get("total_asset"),
            "stock_eval_amount": payload.get("stock_eval_amount"),
        }
        try:
            await self._redis.setex(LIVE_POSITIONS_KEY, 120, json.dumps(snapshot))
            await self._redis.setex(
                MONITOR_STATUS_KEY,
                60,
                json.dumps(
                    {
                        "status": "online",
                        "watching_count": len(positions),
                        "updated_at": snapshot["updated_at"],
                    }
                ),
            )
        except Exception:
            self.last_failure_ts = time.time()
            logger.warning("redis write failed", exc_info=True)
            return 0

        self.last_success_ts = time.time()
        self.last_positions_count = len(positions)
        return len(positions)
