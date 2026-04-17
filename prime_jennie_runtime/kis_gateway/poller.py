"""KIS REST Poller — WebSocket 차단 시 REST 폴링 백업 (async).

WebSocket 접속이 불가능할 때 `get_snapshot` 을 주기적으로 호출하여
`STREAM_PRICES` 에 발행. downstream(Fast loop)은 동일 스트림 소비.

원본: prime_jennie/services/gateway/poller.py (sync → async)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.redis_streams import STREAM_PRICES

from .kis_api import KISApi
from .market_hours import MarketCalendar
from .streamer import PRICE_STREAM_MAXLEN

logger = logging.getLogger(__name__)

# 장외 시간 대기 간격
_OFF_HOURS_SLEEP = 60


class KISRestPoller:
    """REST 폴링으로 시세를 수집 → Redis Stream 발행.

    KISWebSocketStreamer 와 동일한 외형 인터페이스 (duck typing).
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        kis_api: KISApi,
        *,
        polling_interval: float = 3.0,
        calendar: MarketCalendar | None = None,
    ):
        self._redis = redis_client
        self._kis_api = kis_api
        self._polling_interval = polling_interval
        self._calendar = calendar or MarketCalendar()

        self._subscription_codes: set[str] = set()
        self._is_running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def subscription_count(self) -> int:
        return len(self._subscription_codes)

    @property
    def subscribed_codes(self) -> list[str]:
        return sorted(self._subscription_codes)

    async def add_subscriptions(self, codes: list[str]) -> list[str]:
        """구독 종목 추가. 새로 추가된 코드 목록 반환."""
        async with self._lock:
            new_codes = [c for c in codes if c not in self._subscription_codes]
            self._subscription_codes.update(new_codes)
        return new_codes

    async def remove_subscriptions(self, codes: list[str]) -> list[str]:
        """구독 종목 해제. 해제된 코드 목록 반환."""
        async with self._lock:
            removed = [c for c in codes if c in self._subscription_codes]
            self._subscription_codes -= set(removed)
        return removed

    async def start(self, base_url: str | None = None) -> None:
        """폴링 시작. base_url 은 인터페이스 호환용 (무시)."""
        _ = base_url
        if self._is_running:
            logger.warning("Poller already running")
            return

        if not self._subscription_codes:
            logger.warning("No codes to poll")
            return

        self._is_running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "REST Poller started: %d codes, interval=%.1fs",
            len(self._subscription_codes),
            self._polling_interval,
        )

    async def stop(self) -> None:
        """폴링 종료."""
        self._is_running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        logger.info("REST Poller stopped")

    def get_status(self) -> dict[str, Any]:
        """상태 조회."""
        codes = sorted(self._subscription_codes)
        return {
            "is_running": self._is_running,
            "subscription_count": len(codes),
            "codes": codes,
            "mode": "polling",
        }

    async def _poll_loop(self) -> None:
        """폴링 메인 루프 — polling_interval 주기로 전체 종목 순회."""
        while self._is_running:
            if not self._calendar.is_streaming_hours():
                await asyncio.sleep(_OFF_HOURS_SLEEP)
                continue

            cycle_start = asyncio.get_event_loop().time()

            async with self._lock:
                codes = list(self._subscription_codes)

            for code in codes:
                if not self._is_running:
                    return
                try:
                    snapshot = await self._kis_api.get_snapshot(code)
                    await self._redis.xadd(
                        STREAM_PRICES,
                        {
                            "code": snapshot.stock_code,
                            "price": str(snapshot.price),
                            "high": str(snapshot.high_price),
                            "vol": str(snapshot.volume),
                        },
                        maxlen=PRICE_STREAM_MAXLEN,
                        approximate=True,
                    )
                except Exception:
                    logger.warning("Snapshot failed for %s, skipping", code, exc_info=True)

            elapsed = asyncio.get_event_loop().time() - cycle_start
            remaining = self._polling_interval - elapsed
            if remaining > 0 and self._is_running:
                await asyncio.sleep(remaining)
