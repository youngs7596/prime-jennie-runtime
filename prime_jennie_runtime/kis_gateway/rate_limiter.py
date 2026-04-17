"""Asyncio 기반 슬라이딩 윈도우 레이트 리미터.

KIS 계정 기준 글로벌 레이트 리밋 (시세 19/sec, 매매 5/sec). slowapi 는
FastAPI 종속성 있어 유닛 단위 사용이 까다로워 자체 구현.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """고정 1초 윈도우에서 최대 ``rate`` 건만 통과시키는 async 리미터."""

    def __init__(
        self,
        rate: int,
        *,
        window_sec: float = 1.0,
        clock: Callable[[], float] | None = None,
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._window = window_sec
        self._clock = clock or time.monotonic
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> int:
        return self._rate

    async def acquire(self) -> None:
        """윈도우가 가득 차 있으면 가장 오래된 호출이 만료될 때까지 대기."""
        while True:
            async with self._lock:
                now = self._clock()
                # 만료된 timestamp 제거
                while self._timestamps and now - self._timestamps[0] >= self._window:
                    self._timestamps.popleft()

                if len(self._timestamps) < self._rate:
                    self._timestamps.append(now)
                    return

                wait = self._window - (now - self._timestamps[0])

            wait = max(wait, 0.001)
            await asyncio.sleep(wait)

    async def __aenter__(self) -> AsyncRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None
