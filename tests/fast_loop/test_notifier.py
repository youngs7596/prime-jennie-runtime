"""Notifier — Redis stream 발행 + Slack mirror.

핵심:
- env/인자 둘 다 비어있으면 Slack 호출 0회 (기존 동작 유지).
- env 또는 인자 주입되면 critical kind (entry/exit/risk/alert) 만 Slack 전송.
- Slack 전송 실패는 stream 발행에 영향 없음.
"""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response

from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.schemas import (
    GenericAlertNotification,
    RiskLevelChangeNotification,
    TradeNotification,
)
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS


def _entry_notif() -> TradeNotification:
    return TradeNotification(
        kind="entry_filled",
        sheet_id="ps_test",
        ticker="005930",
        side="buy",
        quantity=10,
        price=72_000.0,
        ts=datetime(2026, 5, 11, 9, 30, tzinfo=UTC),
        reason="market",
    )


@pytest.mark.asyncio
async def test_emit_publishes_to_stream_without_slack_env(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        notifier = Notifier(redis)
        msg_id = await notifier.emit(_entry_notif())
        assert isinstance(msg_id, str)
        # stream 에 1건 들어가 있어야 한다.
        length = await redis.xlen(STREAM_NOTIFICATIONS)
        assert length == 1
    finally:
        await notifier.close()
        await redis.aclose()


@pytest.mark.asyncio
async def test_emit_mirrors_entry_to_slack_when_env_set(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/T/X/Y")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("https://hooks.slack.test/T/X/Y").mock(
                return_value=Response(200, text="ok")
            )
            async with httpx.AsyncClient(timeout=5.0) as slack_client:
                notifier = Notifier(redis, slack_client=slack_client)
                await notifier.emit(_entry_notif())
                # exactly one slack POST
                assert route.called
                body = route.calls.last.request.read().decode()
                assert "005930" in body
                assert "Entry" in body or "entry" in body
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_explicit_webhook_arg_overrides_env(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("https://hooks.slack.test/explicit").mock(
                return_value=Response(200, text="ok")
            )
            async with httpx.AsyncClient(timeout=5.0) as slack_client:
                notifier = Notifier(
                    redis,
                    slack_webhook_url="https://hooks.slack.test/explicit",
                    slack_client=slack_client,
                )
                await notifier.emit(_entry_notif())
                assert route.called
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_slack_failure_does_not_break_emit(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/broken")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://hooks.slack.test/broken").mock(
                side_effect=httpx.ConnectError("boom")
            )
            async with httpx.AsyncClient(timeout=5.0) as slack_client:
                notifier = Notifier(redis, slack_client=slack_client)
                # Slack 실패해도 stream 은 정상 발행
                msg_id = await notifier.emit(_entry_notif())
                assert msg_id
                assert await redis.xlen(STREAM_NOTIFICATIONS) == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_slack_received_5xx_does_not_raise(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/serverdown")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://hooks.slack.test/serverdown").mock(
                return_value=Response(500, text="oops")
            )
            async with httpx.AsyncClient(timeout=5.0) as slack_client:
                notifier = Notifier(redis, slack_client=slack_client)
                msg_id = await notifier.emit(_entry_notif())
                assert msg_id
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_risk_level_change_mirrored(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/risk")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notif = RiskLevelChangeNotification(
        prev_level="NORMAL",
        new_level="CAUTION",
        prev_multiplier=1.0,
        new_multiplier=0.75,
        kospi_change_pct=-1.5,
        ts=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
    )
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("https://hooks.slack.test/risk").mock(
                return_value=Response(200, text="ok")
            )
            async with httpx.AsyncClient(timeout=5.0) as slack_client:
                notifier = Notifier(redis, slack_client=slack_client)
                await notifier.emit(notif)
                body = route.calls.last.request.read().decode()
                assert "NORMAL" in body and "CAUTION" in body
                assert "0.75" in body
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_alert_notification_mirrored(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/alert")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    notif = GenericAlertNotification(
        severity="critical",
        title="Monitor stale",
        body="last poll >5m ago",
        ts=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
    )
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("https://hooks.slack.test/alert").mock(
                return_value=Response(200, text="ok")
            )
            async with httpx.AsyncClient(timeout=5.0) as slack_client:
                notifier = Notifier(redis, slack_client=slack_client)
                await notifier.emit(notif)
                body = route.calls.last.request.read().decode()
                assert "Monitor stale" in body
                assert "critical" in body
    finally:
        await redis.aclose()
