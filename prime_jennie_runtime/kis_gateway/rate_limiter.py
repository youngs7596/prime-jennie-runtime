"""Asyncio token bucket 레이트 리미터.

KIS 계정 글로벌 레이트 리밋 (시세 19/sec, 매매 5/sec) 강제. v2 sliding-window
구현은 윈도우 경계에서 burst (예: 1초 끝에 5건 + 다음 1초 시작에 5건 = 200ms
안에 10건) 가 가능해 KIS 측 초당 한도 (`EGW00201`) 를 트립하는 사고가
2026-05-13 발생. Token bucket 으로 변경하여 capacity 만큼의 burst 만 허용
(평균 rate/sec 유지).

생성자 시그니처는 기존 sliding-window 와 호환 (rate, window_sec, clock).
``capacity`` = ``rate`` (별도 인자 없음) — KIS 한도 그대로 burst 한도로 사용.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Token bucket 기반 async 리미터.

    - ``capacity`` = ``rate`` (생성자 인자로 별도 노출 안 함)
    - refill: ``rate / window_sec`` tokens/sec — 평균 rate 유지
    - acquire: 토큰 1개 소비. 부족하면 1개가 채워질 때까지 sleep
    """

    def __init__(
        self,
        rate: int,
        *,
        window_sec: float = 1.0,
        clock: Callable[[], float] | None = None,
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self._rate = rate
        self._window = window_sec
        self._capacity = float(rate)
        self._refill_per_sec = rate / window_sec
        self._tokens = float(rate)
        self._clock = clock or time.monotonic
        self._last_refill = self._clock()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> int:
        return self._rate

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_sec)
            self._last_refill = now

    async def acquire(self) -> None:
        """토큰 1개 소비. 부족하면 충전될 때까지 sleep."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # 토큰 1개가 채워지는 데 필요한 시간
                needed = 1.0 - self._tokens
                wait = needed / self._refill_per_sec

            wait = max(wait, 0.001)
            await asyncio.sleep(wait)

    async def __aenter__(self) -> AsyncRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None
