"""AsyncRateLimiter 테스트."""

from __future__ import annotations

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
