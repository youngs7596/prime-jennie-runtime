"""Agent Coordinator — Event Bus 발행 helper.

design: .ai/designs/2026-05-14-agent-coordinator.md §3 Layer C / §8 Stage 1

이 모듈의 역할은 단 하나: Pydantic `CoordinatorEvent` 를 받아 Redis Stream
``coordinator:events`` 로 publish. 다른 agent (slow_loop / fast_loop / executor /
strategy) 가 import 해서 호출하는 단일 진입점이다.

Stage 1 정책:
  - fail-loud: 발행 실패 시 raise. listener 가 안 떠 있어도 stream 에 적재만
    되면 backlog 로 따라잡을 수 있으므로 best-effort 가 아니라 strict 가 더 안전.
  - MAXLEN approximate: ~100k 로 자연 trim. 운영 중 자동 청소 (Stage 2 에서
    별도 retention job 도입 검토).
  - payload 직렬화는 ``event.model_dump_json()`` 1개 필드로 통일 — listener 와
    contract 고정. 향후 schema 변경 시 events.py 에서만 수정.

기존 agent path 침해 금지:
  - 호출부는 publish 실패에 try/except 를 두는 것이 권장 — 이 모듈은 raise 하지만,
    매매 critical path 가 멈추면 안 되는 호출부 (entry/exit executor) 는 자체
    fallback 으로 swallow + 로그 처리해라. 그 정책은 호출부 책임.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ─── Contract (다른 agent 와 공유) ───────────────────────────────────
COORDINATOR_STREAM = "coordinator:events"
COORDINATOR_STREAM_MAXLEN = 100_000


async def publish_event(
    redis: aioredis.Redis,
    event: BaseModel,
    *,
    stream_key: str = COORDINATOR_STREAM,
    maxlen: int = COORDINATOR_STREAM_MAXLEN,
) -> str:
    """Coordinator event 를 ``coordinator:events`` 에 publish.

    Args:
        redis: 비동기 Redis client.
        event: ``CoordinatorEvent`` discriminated union 중 하나. 타입 힌트는
            BaseModel 로 둬서 호출부가 매번 cast 하지 않아도 되게 했지만,
            실질적으론 events.py 의 9 종 이벤트 중 하나여야 한다 (listener
            쪽에서 model_validate 로 강제 검증).
        stream_key: 발행 대상 stream key. 기본은 운영 ``coordinator:events``.
            integration test 는 임시 key (예: ``coordinator:events:test-<uuid>``)
            로 override 해서 운영 stream 오염을 피한다.
        maxlen: XADD MAXLEN approximate 값. 기본은 100k.

    Returns:
        XADD 가 부여한 stream message id (decoded str).

    Raises:
        redis 예외 (ConnectionError 등). 호출부에서 매매 critical path 면
        catch 해서 로그만 남기고 진행해라. 본 함수는 fail-loud.
    """
    payload = event.model_dump_json()
    data = {"payload": payload}
    msg_id = await redis.xadd(
        stream_key,
        data,
        maxlen=maxlen,
        approximate=True,
    )
    mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
    logger.debug(
        "coordinator publish: kind=%s id=%s",
        getattr(event, "event_kind", "?"),
        mid,
    )
    return mid


__all__ = [
    "COORDINATOR_STREAM",
    "COORDINATOR_STREAM_MAXLEN",
    "publish_event",
]
