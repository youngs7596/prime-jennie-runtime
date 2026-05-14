"""CoordinatorWatchPoller — DLQ growth + listener heartbeat staleness."""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
from pydantic import BaseModel

from prime_jennie_runtime.monitor.coordinator_watch import (
    DLQ_STREAM_KEY,
    LISTENER_HEARTBEAT_KEY,
    WATCH_HEARTBEAT_KEY,
    CoordinatorWatchPoller,
)


class _CaptureNotifier:
    """Notifier 흉내 — emit 된 GenericAlertNotification 을 리스트에 모은다."""

    def __init__(self) -> None:
        self.emitted: list[BaseModel] = []

    async def emit(self, notification: BaseModel) -> str:
        self.emitted.append(notification)
        return "stub-id"

    async def close(self) -> None:
        pass


def _hb_payload() -> str:
    return json.dumps({"service": "coordinator-listener", "pid": 1})


@pytest.mark.asyncio
async def test_baseline_clean_no_alert():
    """첫 tick — DLQ 0, listener heartbeat 정상 → 알림 0."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CaptureNotifier()
    try:
        await redis.setex(LISTENER_HEARTBEAT_KEY, 90, _hb_payload())
        poller = CoordinatorWatchPoller(redis_client=redis, notifier=notifier)
        await poller.tick_once()
        assert notifier.emitted == []
        assert poller.last_dlq_xlen_observed == 0
        assert poller.last_listener_heartbeat_ok is True
        # 자체 heartbeat 도 갱신
        raw = await redis.get(WATCH_HEARTBEAT_KEY)
        assert raw is not None
        assert json.loads(raw)["status"] == "online"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_baseline_dlq_nonzero_emits_warning_once():
    """첫 tick 에 DLQ 가 비어있지 않으면 warning 1회. 두 번째 tick 에선 안 함."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CaptureNotifier()
    try:
        await redis.setex(LISTENER_HEARTBEAT_KEY, 90, _hb_payload())
        # baseline 2 row pre-existing
        await redis.xadd(DLQ_STREAM_KEY, {"src_id": "x-1", "payload": "{}"})
        await redis.xadd(DLQ_STREAM_KEY, {"src_id": "x-2", "payload": "{}"})

        poller = CoordinatorWatchPoller(redis_client=redis, notifier=notifier)
        await poller.tick_once()
        assert len(notifier.emitted) == 1
        n = notifier.emitted[0]
        assert n.severity == "warning"
        assert n.metadata["phase"] == "baseline"
        assert n.metadata["dlq_xlen"] == 2

        # 두 번째 tick — 변화 없음 → 추가 알림 없음
        await poller.tick_once()
        assert len(notifier.emitted) == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_dlq_growth_emits_critical_with_delta():
    """0 → +N delta 마다 critical alert. 동일 수준 유지 시 추가 알림 없음."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CaptureNotifier()
    try:
        await redis.setex(LISTENER_HEARTBEAT_KEY, 90, _hb_payload())
        poller = CoordinatorWatchPoller(redis_client=redis, notifier=notifier)

        # tick 1: baseline 0
        await poller.tick_once()
        assert notifier.emitted == []

        # tick 2: DLQ 에 1 추가 → critical
        await redis.xadd(DLQ_STREAM_KEY, {"src_id": "y-1", "payload": "{}"})
        await poller.tick_once()
        assert len(notifier.emitted) == 1
        assert notifier.emitted[-1].severity == "critical"
        assert notifier.emitted[-1].metadata["delta"] == 1

        # tick 3: 동일 (1 row) → 추가 알림 없음
        await poller.tick_once()
        assert len(notifier.emitted) == 1

        # tick 4: DLQ 에 2 더 추가 → critical (delta=2)
        await redis.xadd(DLQ_STREAM_KEY, {"src_id": "y-2", "payload": "{}"})
        await redis.xadd(DLQ_STREAM_KEY, {"src_id": "y-3", "payload": "{}"})
        await poller.tick_once()
        assert len(notifier.emitted) == 2
        assert notifier.emitted[-1].metadata["delta"] == 2
        assert notifier.emitted[-1].metadata["dlq_xlen_cur"] == 3
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_listener_heartbeat_missing_threshold_then_recovery():
    """heartbeat 가 threshold tick 연속 missing 이면 critical, 회복 시 info recovery."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CaptureNotifier()
    try:
        # heartbeat 미존재 상태로 시작
        poller = CoordinatorWatchPoller(
            redis_client=redis,
            notifier=notifier,
            heartbeat_threshold=3,
        )

        # tick 1, 2 — failure 누적, alert 없음
        await poller.tick_once()
        await poller.tick_once()
        assert notifier.emitted == []
        assert poller._heartbeat_consecutive_failures == 2

        # tick 3 — threshold 도달 → critical 1회
        await poller.tick_once()
        assert len(notifier.emitted) == 1
        n = notifier.emitted[-1]
        assert n.severity == "critical"
        assert n.metadata["consecutive_failures"] == 3

        # tick 4 — 여전히 missing, alert_firing 상태라 추가 알림 없음
        await poller.tick_once()
        assert len(notifier.emitted) == 1

        # heartbeat 복귀 → 다음 tick 에서 info recovery 1회
        await redis.setex(LISTENER_HEARTBEAT_KEY, 90, _hb_payload())
        await poller.tick_once()
        assert len(notifier.emitted) == 2
        rec = notifier.emitted[-1]
        assert rec.severity == "info"
        assert "회복" in rec.title

        # 정상 상태 유지 — 추가 알림 없음
        await poller.tick_once()
        assert len(notifier.emitted) == 2
        assert poller._heartbeat_consecutive_failures == 0
        assert poller._heartbeat_alert_firing is False
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_self_heartbeat_reflects_state():
    """monitor:coordinator_watch_heartbeat 가 매 tick 마다 최신 상태를 노출."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CaptureNotifier()
    try:
        # heartbeat 없음
        poller = CoordinatorWatchPoller(redis_client=redis, notifier=notifier)
        await poller.tick_once()
        raw = await redis.get(WATCH_HEARTBEAT_KEY)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["status"] == "listener_stale"
        assert payload["last_dlq_xlen"] == 0

        # heartbeat 추가 + DLQ 에 1 row → status online + xlen 1
        await redis.setex(LISTENER_HEARTBEAT_KEY, 90, _hb_payload())
        await redis.xadd(DLQ_STREAM_KEY, {"src_id": "z-1", "payload": "{}"})
        await poller.tick_once()
        payload = json.loads(await redis.get(WATCH_HEARTBEAT_KEY))
        assert payload["status"] == "online"
        assert payload["last_dlq_xlen"] == 1
    finally:
        await redis.aclose()
