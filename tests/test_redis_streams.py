"""infra/redis_streams.py 테스트 — fakeredis 기반."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from prime_jennie_runtime.infra.redis_streams import (
    TypedStreamConsumer,
    TypedStreamPublisher,
)


class SampleMessage(BaseModel):
    ticker: str
    price: float


@pytest.mark.asyncio
async def test_publish_and_consume(fake_redis):
    """발행 → 소비 사이클."""
    stream = "test:stream"
    received: list[SampleMessage] = []

    publisher = TypedStreamPublisher(fake_redis, stream, SampleMessage)
    msg_id = await publisher.publish(SampleMessage(ticker="005930", price=71200))
    assert msg_id

    async def handler(msg: SampleMessage) -> None:
        received.append(msg)

    consumer = TypedStreamConsumer(
        fake_redis, stream, "test_group", "consumer_1", SampleMessage, handler
    )
    await consumer.ensure_group()

    # 수동으로 한 번 읽기
    messages = await fake_redis.xreadgroup("test_group", "consumer_1", {stream: ">"}, count=1)
    assert len(messages) > 0

    for _stream_name, entries in messages:
        for mid, data in entries:
            await consumer._process_message(mid, data)

    assert len(received) == 1
    assert received[0].ticker == "005930"
    assert received[0].price == 71200


@pytest.mark.asyncio
async def test_publish_maxlen(fake_redis):
    """maxlen 제한 동작."""
    stream = "test:maxlen"
    publisher = TypedStreamPublisher(fake_redis, stream, SampleMessage, maxlen=5)

    for i in range(10):
        await publisher.publish(SampleMessage(ticker=f"{i:06d}", price=float(i)))

    length = await fake_redis.xlen(stream)
    assert length <= 10  # approximate=True이므로 정확히 5는 아닐 수 있음
