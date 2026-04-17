"""NotificationConsumer 테스트 — fakeredis + FakeTelegramBot."""

from __future__ import annotations

from datetime import datetime

import pytest

from prime_jennie_runtime.fast_loop.schemas import (
    GenericAlertNotification,
    RiskLevelChangeNotification,
    TradeNotification,
)
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_NOTIFICATIONS,
    TypedStreamPublisher,
)
from prime_jennie_runtime.telegram_bot.consumer import NotificationConsumer

from .fakes import FakeTelegramBot

TS = datetime(2026, 4, 17, 11, 0, 0)


@pytest.mark.asyncio
async def test_consumer_dispatches_three_kinds(fake_redis):
    """entry/risk/alert 3종 XADD → consumer가 3건 bot에 전달."""
    trade_pub = TypedStreamPublisher(fake_redis, STREAM_NOTIFICATIONS, TradeNotification)
    risk_pub = TypedStreamPublisher(fake_redis, STREAM_NOTIFICATIONS, RiskLevelChangeNotification)
    alert_pub = TypedStreamPublisher(fake_redis, STREAM_NOTIFICATIONS, GenericAlertNotification)

    await trade_pub.publish(
        TradeNotification(
            kind="entry_filled",
            sheet_id="s1",
            ticker="005930",
            side="buy",
            quantity=1,
            price=71000.0,
            ts=TS,
        )
    )
    await risk_pub.publish(
        RiskLevelChangeNotification(
            prev_level="NORMAL",
            new_level="CAUTION",
            prev_multiplier=1.0,
            new_multiplier=0.8,
            ts=TS,
        )
    )
    await alert_pub.publish(
        GenericAlertNotification(
            severity="warning",
            title="stale",
            body="price feed stale",
            ts=TS,
        )
    )

    bot = FakeTelegramBot()
    consumer = NotificationConsumer(
        client=fake_redis,
        stream=STREAM_NOTIFICATIONS,
        group="group_telegram_test",
        consumer="c1",
        bot=bot,
    )
    processed = await consumer.process_one()
    assert processed == 3
    assert len(bot.sent_messages) == 3
    # 각 타입별 특징적 문자열 확인
    joined = "\n---\n".join(bot.sent_messages)
    assert "[진입 체결]" in joined
    assert "CAUTION" in joined
    assert "[WARNING]" in joined


@pytest.mark.asyncio
async def test_consumer_unknown_kind_is_discarded(fake_redis):
    """payload.kind가 미지여도 예외 없이 폐기 (bot 호출 없음)."""
    stream = STREAM_NOTIFICATIONS
    await fake_redis.xadd(stream, {"payload": '{"kind": "nonexistent", "foo": "bar"}'})

    bot = FakeTelegramBot()
    consumer = NotificationConsumer(
        client=fake_redis,
        stream=stream,
        group="group_unknown_test",
        consumer="c1",
        bot=bot,
    )
    processed = await consumer.process_one()
    assert processed == 1
    assert bot.sent_messages == []


@pytest.mark.asyncio
async def test_consumer_invalid_json_is_discarded(fake_redis):
    """payload가 JSON이 아니어도 예외 없이 폐기."""
    stream = STREAM_NOTIFICATIONS
    await fake_redis.xadd(stream, {"payload": "not-json"})

    bot = FakeTelegramBot()
    consumer = NotificationConsumer(
        client=fake_redis,
        stream=stream,
        group="group_invalid_test",
        consumer="c1",
        bot=bot,
    )
    processed = await consumer.process_one()
    assert processed == 1
    assert bot.sent_messages == []
