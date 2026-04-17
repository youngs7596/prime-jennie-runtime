"""Async 서킷 브레이커 — pybreaker 는 sync 전용이라 래핑 대신 직접 구현.

상태 전이:
  CLOSED  ── fail_count >= fail_max ──▶  OPEN
  OPEN    ── reset_sec 경과 ──▶             HALF_OPEN
  HALF_OPEN ── 성공 ──▶ CLOSED / 실패 ──▶  OPEN
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """서킷이 OPEN 상태여서 호출을 거부했을 때 발생."""


class AsyncCircuitBreaker:
    """Async circuit breaker (KIS Gateway 전용, v2 pybreaker 대체)."""

    def __init__(
        self,
        *,
        fail_max: int = 20,
        reset_sec: float = 60.0,
        clock: Callable[[], float] | None = None,
    ):
        if fail_max <= 0:
            raise ValueError("fail_max must be positive")
        self._fail_max = fail_max
        self._reset_sec = reset_sec
        self._clock = clock or time.monotonic

        self._state: str = STATE_CLOSED
        self._fail_count = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def fail_count(self) -> int:
        return self._fail_count

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """``fn`` 을 호출하며 실패/성공을 집계. OPEN 상태면 즉시 거부."""
        async with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == STATE_OPEN:
                raise CircuitBreakerError("Circuit breaker is OPEN")

        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()
            return result

    def _maybe_transition_to_half_open(self) -> None:
        if self._state == STATE_OPEN and self._clock() - self._opened_at >= self._reset_sec:
            logger.warning("Circuit breaker: OPEN -> HALF_OPEN")
            self._state = STATE_HALF_OPEN

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state == STATE_HALF_OPEN:
                logger.warning("Circuit breaker: HALF_OPEN -> CLOSED")
                self._state = STATE_CLOSED
            self._fail_count = 0

    async def _record_failure(self) -> None:
        async with self._lock:
            if self._state == STATE_HALF_OPEN:
                logger.warning("Circuit breaker: HALF_OPEN -> OPEN")
                self._state = STATE_OPEN
                self._opened_at = self._clock()
                return

            self._fail_count += 1
            if self._fail_count >= self._fail_max and self._state == STATE_CLOSED:
                logger.warning("Circuit breaker: CLOSED -> OPEN (fail_count=%d)", self._fail_count)
                self._state = STATE_OPEN
                self._opened_at = self._clock()
