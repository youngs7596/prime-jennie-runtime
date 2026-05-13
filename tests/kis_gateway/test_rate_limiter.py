"""AsyncRateLimiter 테스트."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.kis_gateway.rate_limiter import AsyncRateLimiter


class FakeClock:
    """단조 증가 가상 시계. tick() 로 시간 전진."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, delta: float) -> None:
        self.now += delta


async def test_within_limit_acquires_immediately():
    clock = FakeClock()
    limiter = AsyncRateLimiter(rate=3, window_sec=1.0, clock=clock)

    # 3건 연속 acquire 가능
    for _ in range(3):
        await limiter.acquire()


async def test_over_limit_triggers_wait(monkeypatch):
    """4번째 acquire 는 대기. asyncio.sleep 를 패치하여 실제 대기 없이 검증."""
    clock = FakeClock()
    limiter = AsyncRateLimiter(rate=3, window_sec=1.0, clock=clock)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        # 시간이 흐른 것처럼 시계 전진 → 첫 timestamp 가 만료되도록
        clock.tick(delay)

    import prime_jennie_runtime.kis_gateway.rate_limiter as rl

    monkeypatch.setattr(rl.asyncio, "sleep", fake_sleep)

    for _ in range(3):
        await limiter.acquire()

    assert sleep_calls == []
    await limiter.acquire()
    assert len(sleep_calls) == 1
    assert sleep_calls[0] > 0


async def test_expired_entries_are_evicted():
    clock = FakeClock()
    limiter = AsyncRateLimiter(rate=2, window_sec=1.0, clock=clock)

    await limiter.acquire()
    await limiter.acquire()
    # 1.5초 뒤 — 윈도우가 비어 있어 추가 acquire 가능
    clock.tick(1.5)
    await limiter.acquire()  # 대기 없이 통과 (내부적으로 timestamp 제거)


async def test_context_manager_usage():
    clock = FakeClock()
    limiter = AsyncRateLimiter(rate=1, window_sec=1.0, clock=clock)
    async with limiter:
        pass


async def test_token_bucket_blocks_burst_at_window_boundary(monkeypatch):
    """Token bucket 으로 sliding-window 윈도우 경계 burst 사고 방지.

    rate=5/sec 일 때 5건 burst 후 6번째는 200ms 대기. sliding-window 였다면
    윈도우 경계에서 추가 5건 통과 가능했음 (5-13 KIS throttle 사고 원인 중 하나).
    """
    clock = FakeClock()
    limiter = AsyncRateLimiter(rate=5, window_sec=1.0, clock=clock)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        clock.tick(delay)

    import prime_jennie_runtime.kis_gateway.rate_limiter as rl

    monkeypatch.setattr(rl.asyncio, "sleep", fake_sleep)

    # 첫 5건 burst — 즉시 통과
    for _ in range(5):
        await limiter.acquire()
    assert sleep_calls == []

    # 6번째 — 토큰 충전 대기 (≈ 1/5 = 0.2초)
    await limiter.acquire()
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == pytest.approx(0.2, abs=0.01)


async def test_steady_state_average_rate():
    """충분히 시간이 지나면 평균 rate/sec 유지 (token bucket capacity 한도 안에서)."""
    clock = FakeClock()
    limiter = AsyncRateLimiter(rate=5, window_sec=1.0, clock=clock)

    # 처음 5건 burst
    for _ in range(5):
        await limiter.acquire()
    # 1초 후 — capacity 만큼 다시 충전
    clock.tick(1.0)
    for _ in range(5):
        await limiter.acquire()
