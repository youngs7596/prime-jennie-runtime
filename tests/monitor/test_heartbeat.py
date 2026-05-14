"""Monitor heartbeat + alert escalation.

LivePositionsPoller 가 연속 실패 시:
- consecutive_failures 카운터 증가
- HEARTBEAT_KEY 에 degraded 상태 쓰기
- 임계 (default 3) 도달 시 notifier 로 critical alert 발행 (1회만)
회복 시:
- consecutive_failures 0 리셋
- HEARTBEAT_KEY 가 online 으로 갱신
- 직전이 firing 상태였다면 info alert 한 번 더 발행
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response
from pydantic import BaseModel

from prime_jennie_runtime.monitor.poller import (
    HEARTBEAT_KEY,
    LivePositionsPoller,
)


class _CapturingNotifier:
    """Notifier 와 동일한 emit(notification) 인터페이스 — 발행 내용만 캡처."""

    def __init__(self) -> None:
        self.emissions: list[BaseModel] = []

    async def emit(self, notification: BaseModel) -> str:
        self.emissions.append(notification)
        return f"capture-{len(self.emissions)}"

    async def close(self) -> None:  # pragma: no cover - parity helper
        return None


def _balance_response(positions: list[dict[str, Any]] | None = None) -> Response:
    return Response(
        200,
        json={
            "cash_balance": 100_000,
            "total_asset": 200_000,
            "stock_eval_amount": 100_000,
            "positions": positions or [],
        },
    )


@pytest.mark.asyncio
async def test_failure_counter_and_heartbeat_writes():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CapturingNotifier()
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(
                side_effect=httpx.ConnectError("boom")
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                    notifier=notifier,  # type: ignore[arg-type]
                    alert_threshold=3,
                )
                # 1회 실패
                await poller.tick_once()
                assert poller.consecutive_failures == 1
                raw = await redis.get(HEARTBEAT_KEY)
                hb = json.loads(raw)
                assert hb["status"] == "degraded"
                assert hb["consecutive_failures"] == 1
                assert hb["alert_firing"] is False
                assert notifier.emissions == []

                # 2회 실패 — 아직 임계 미만
                await poller.tick_once()
                assert poller.consecutive_failures == 2
                assert notifier.emissions == []

                # 3회 실패 → 임계 도달, alert 1건 발행
                await poller.tick_once()
                assert poller.consecutive_failures == 3
                assert len(notifier.emissions) == 1
                alert = notifier.emissions[0]
                assert alert.kind == "alert"
                assert alert.severity == "critical"
                assert "Monitor poll failing" in alert.title

                hb_after = json.loads(await redis.get(HEARTBEAT_KEY))
                assert hb_after["alert_firing"] is True

                # 4회 실패 — alert 재발행 없음 (rate-limit)
                await poller.tick_once()
                assert len(notifier.emissions) == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_recovery_emits_info_alert_and_resets_counter():
    """3회 실패 후 1회 성공 → counter 0, info alert 한 번."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CapturingNotifier()
    try:
        # 첫 3 호출은 fail, 4번째는 ok — respx 의 side_effect list 로 sequencing
        responses = [
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
            _balance_response([{"stock_code": "005930", "quantity": 1}]),
        ]
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(side_effect=responses)
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                    notifier=notifier,  # type: ignore[arg-type]
                    alert_threshold=3,
                )
                # 3회 실패
                for _ in range(3):
                    await poller.tick_once()
                assert poller.consecutive_failures == 3
                assert len(notifier.emissions) == 1
                assert notifier.emissions[0].severity == "critical"

                # 회복
                count = await poller.tick_once()
                assert count == 1
                assert poller.consecutive_failures == 0
                # 두 번째 알림 = info recovered
                assert len(notifier.emissions) == 2
                recovered = notifier.emissions[1]
                assert recovered.severity == "info"
                assert "recovered" in recovered.title.lower()

                hb = json.loads(await redis.get(HEARTBEAT_KEY))
                assert hb["status"] == "online"
                assert hb["consecutive_failures"] == 0
                assert hb["alert_firing"] is False
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_no_alert_below_threshold_then_success():
    """3회 미만 실패 후 회복 — alert 발행 없음."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notifier = _CapturingNotifier()
    try:
        responses = [
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
            _balance_response(),
        ]
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(side_effect=responses)
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                    notifier=notifier,  # type: ignore[arg-type]
                    alert_threshold=3,
                )
                for _ in range(3):
                    await poller.tick_once()
                assert poller.consecutive_failures == 0
                assert notifier.emissions == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_without_notifier_logs_but_no_crash():
    """notifier=None 일 때 임계 도달해도 정상 동작 (로깅만)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(
                side_effect=httpx.ConnectError("boom")
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                    notifier=None,
                    alert_threshold=2,
                )
                for _ in range(3):
                    await poller.tick_once()
                assert poller.consecutive_failures == 3
                # heartbeat 는 계속 갱신
                hb = json.loads(await redis.get(HEARTBEAT_KEY))
                assert hb["status"] == "degraded"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_heartbeat_payload_shape():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(return_value=_balance_response())
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                )
                await poller.tick_once()
                hb = json.loads(await redis.get(HEARTBEAT_KEY))
                # 필수 필드
                for key in (
                    "status",
                    "last_success_ts",
                    "last_failure_ts",
                    "consecutive_failures",
                    "alert_firing",
                    "reason",
                    "updated_at",
                ):
                    assert key in hb
                # ISO 형식 검증 (loose)
                datetime.fromisoformat(hb["updated_at"].replace("Z", "+00:00"))
                assert hb["status"] == "online"
                assert hb["consecutive_failures"] == 0
                # last_success_ts 는 epoch float
                assert isinstance(hb["last_success_ts"], int | float)
                # last_failure_ts 는 아직 None
                assert hb["last_failure_ts"] is None
    finally:
        # UTC dummy reference for unused-import guard
        _ = UTC
        await redis.aclose()
