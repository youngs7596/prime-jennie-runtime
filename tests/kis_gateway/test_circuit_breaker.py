"""AsyncCircuitBreaker 상태 전이 테스트."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.kis_gateway.circuit_breaker import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    AsyncCircuitBreaker,
    CircuitBreakerError,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, delta: float) -> None:
        self.now += delta


async def _ok() -> str:
    return "ok"


async def _boom() -> None:
    raise RuntimeError("boom")


async def test_closed_allows_calls():
    breaker = AsyncCircuitBreaker(fail_max=3, reset_sec=10.0)
    result = await breaker.call(_ok)
    assert result == "ok"
    assert breaker.state == STATE_CLOSED


async def test_opens_after_fail_max():
    breaker = AsyncCircuitBreaker(fail_max=3, reset_sec=10.0)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
    assert breaker.state == STATE_OPEN


async def test_open_rejects_calls():
    clock = FakeClock()
    breaker = AsyncCircuitBreaker(fail_max=2, reset_sec=10.0, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
    assert breaker.state == STATE_OPEN

    with pytest.raises(CircuitBreakerError):
        await breaker.call(_ok)


async def test_open_to_half_open_after_reset():
    clock = FakeClock()
    breaker = AsyncCircuitBreaker(fail_max=2, reset_sec=10.0, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
    assert breaker.state == STATE_OPEN

    clock.tick(11.0)
    # 다음 호출 시 HALF_OPEN 으로 전이 + 성공 시 CLOSED
    result = await breaker.call(_ok)
    assert result == "ok"
    assert breaker.state == STATE_CLOSED


async def test_half_open_failure_reopens():
    clock = FakeClock()
    breaker = AsyncCircuitBreaker(fail_max=2, reset_sec=10.0, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
    clock.tick(11.0)
    # 첫 HALF_OPEN 호출에서 실패 → OPEN 복귀
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.state == STATE_OPEN


async def test_half_open_state_transition_is_visible():
    clock = FakeClock()
    breaker = AsyncCircuitBreaker(fail_max=1, reset_sec=5.0, clock=clock)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.state == STATE_OPEN

    clock.tick(6.0)
    # call() 에서 내부 transition 발생 — 현재 메서드에서는 먼저 HALF_OPEN 으로
    # 전이된 뒤 fn 이 호출된다. fn 이 성공하면 CLOSED 로 전이.
    # HALF_OPEN state 자체는 fn 실행 중에 관찰 가능.
    observed: list[str] = []

    async def _probe() -> str:
        observed.append(breaker.state)
        return "done"

    assert await breaker.call(_probe) == "done"
    assert observed == [STATE_HALF_OPEN]
