"""Asyncio token bucket 레이트 리미터.

KIS 계정 글로벌 레이트 리밋 (시세 19/sec, 매매 5/sec) 강제. v2 sliding-window
구현은 윈도우 경계에서 burst (예: 1초 끝에 5건 + 다음 1초 시작에 5건 = 200ms
안에 10건) 가 가능해 KIS 측 초당 한도 (`EGW00201`) 를 트립하는 사고가
2026-05-13 발생. Token bucket 으로 변경하여 capacity 만큼의 burst 만 허용
(평균 rate/sec 유지).

생성자 시그니처는 기존 sliding-window 와 호환 (rate, window_sec, clock).
``capacity`` 기본값은 ``rate`` — KIS 한도 그대로 burst 한도로 사용.

2026-06-03 추가: ``capacity`` 를 rate 보다 작게 주면 순간 burst 를 더 좁게 묶을 수
있다. capacity=rate 이면 "가득 찬 bucket 에서 rate 만큼 즉시 소진 + 같은 1초 안에
refill 로 rate 만큼 더" = 한 윈도우에 최대 2×rate 가 나가는 구조라, KIS 처럼 측정
윈도우가 빡빡한 상대에는 capacity 를 줄여야 한 윈도우 최대치 (capacity + rate) 를
상대 한도 아래로 보장할 수 있다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Token bucket 기반 async 리미터.

    - ``capacity`` 기본값 = ``rate``. 작게 주면 순간 burst 한도를 좁힘
    - refill: ``rate / window_sec`` tokens/sec — 평균 rate 유지
    - acquire: 토큰 1개 소비. 부족하면 1개가 채워질 때까지 sleep
    """

    def __init__(
        self,
        rate: int,
        *,
        window_sec: float = 1.0,
        capacity: int | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        if capacity is not None and capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = rate
        self._window = window_sec
        self._capacity = float(capacity if capacity is not None else rate)
        self._refill_per_sec = rate / window_sec
        self._tokens = self._capacity
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
