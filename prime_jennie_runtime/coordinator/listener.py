"""Agent Coordinator — Event Bus listener.

design: .ai/designs/2026-05-14-agent-coordinator.md §3 Layer C / §8 Stage 1
schema: migrations/017_coordinator_logs.sql (event_log)
events: prime_jennie_runtime/coordinator/events.py (Discriminated Union)

역할: ``coordinator:events`` Redis Stream 을 consumer group 으로 구독해서
받은 이벤트를 ``event_log`` 테이블에 INSERT 한다. Stage 1 은 read-only
archive 이며 기존 agent path 는 건드리지 않는다.

ACK 정책:
  - XREADGROUP 으로 메시지를 읽고, JSON parse + Pydantic validate + INSERT 가
    모두 성공한 후에 XACK. at-least-once delivery.
  - validate / INSERT 실패 시 ACK 하지 않고 다음 메시지로 진행한다. Pending
    list 에 남아 다음 polling 사이클에 재처리.
  - **idempotency**: event_log.stream_msg_id 가 UNIQUE — 재배달 시 INSERT 가
    ON CONFLICT DO NOTHING 으로 swallow. ACK 실패가 row 중복을 만들지 않음.
  - **DLQ**: 주기적 XPENDING_RANGE 검사 — delivery_count >= _DLQ_MAX_DELIVERIES
    인 메시지는 별도 stream (``<stream>:dlq``) 으로 이관 후 ACK. 영구 깨진
    메시지가 backlog 를 막지 않도록.

shutdown:
  - ``stop_event`` (asyncio.Event) 가 set 되면 graceful 종료.
  - cancellation 도 처리 (asyncio.CancelledError).

backoff:
  - Redis ConnectionError 발생 시 5초 대기 후 재시도 (infra/redis_streams.py
    의 기존 패턴 일치).

결정론 layer:
  - 본 모듈은 Coordinator 의 결정론 코드. Pydantic schema 검증을 통과 못한
    이벤트는 reject + 로그만 남기고 silently 진행. prompt 로 결정론 제어
    하지 않는다 (memory: feedback_prompt_control_limit).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

import redis.asyncio as aioredis
from pydantic import TypeAdapter, ValidationError

from prime_jennie_runtime.coordinator.events import CoordinatorEvent
from prime_jennie_runtime.coordinator.publish import COORDINATOR_STREAM

logger = logging.getLogger(__name__)

# ─── Contract (다른 agent 와 공유 — publish.py 의 stream 이름과 짝) ───
COORDINATOR_GROUP = "coordinator_listener"
COORDINATOR_CONSUMER_ENV = "COORDINATOR_CONSUMER_NAME"

# 폴링 / 배치 파라미터
_BLOCK_MS = 5_000
_BATCH_SIZE = 32
_RECONNECT_BACKOFF_SEC = 5

# DLQ 정책 — delivery_count 가 이 값 이상이면 별도 stream 으로 격리 후 ACK.
_DLQ_SUFFIX = ":dlq"
_DLQ_MAX_DELIVERIES = 5
_DLQ_MAXLEN = 10_000
# pending check 주기 — 매 N 사이클마다 XPENDING_RANGE 실행 (운영 부담 균형).
_PENDING_CHECK_INTERVAL = 12  # 약 1분 (block=5s × 12)

# event_log INSERT — payload_json 은 ::jsonb 캐스트 (asyncpg 패턴은
# news_pipeline_kor/adapters/pg_event_repo.py 참고). stream_msg_id 의 UNIQUE
# partial index 에 ON CONFLICT 로 idempotency 보장 (재배달 → DO NOTHING).
_INSERT_EVENT_LOG = (
    "INSERT INTO event_log "
    "(event_kind, subject_id, ticker, payload_json, correlation_id, "
    " decision_id, stream_msg_id, occurred_at) "
    "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8) "
    "ON CONFLICT (stream_msg_id) WHERE stream_msg_id IS NOT NULL DO NOTHING"
)

# Discriminated Union 의 validate 는 TypeAdapter 경유로만 동작한다.
_EVENT_ADAPTER: TypeAdapter[CoordinatorEvent] = TypeAdapter(CoordinatorEvent)


def _resolve_consumer_name() -> str:
    """consumer name 결정 — env 우선, fallback hostname."""
    env = os.environ.get(COORDINATOR_CONSUMER_ENV, "").strip()
    if env:
        return env
    try:
        host = socket.gethostname() or "unknown"
    except OSError:
        host = "unknown"
    return f"coordinator-{host}"


def _subject_id_for(event: Any) -> str | None:
    """이벤트 종류별 subject_id 추출 규칙.

    명세:
      - sheet_id 가 있으면 그것 (lifecycle 이벤트 8 종)
      - 없으면 policy_changed.policy_name
      - 둘 다 없으면 NULL (external_event 등)
    """
    sheet_id = getattr(event, "sheet_id", None)
    if sheet_id:
        return str(sheet_id)
    policy_name = getattr(event, "policy_name", None)
    if policy_name:
        return str(policy_name)
    return None


async def _ensure_group(
    redis: aioredis.Redis,
    stream_key: str,
    consumer_group: str,
) -> None:
    """consumer group 생성 (BUSYGROUP 무시, MKSTREAM 으로 stream 도 보장).

    Redis 가 아직 준비 안 됐을 때 재시도 (infra/redis_streams.py 패턴 차용).
    """
    for attempt in range(30):
        try:
            await redis.xgroup_create(stream_key, consumer_group, id="0", mkstream=True)
            logger.info(
                "coordinator listener: group created stream=%s group=%s",
                stream_key,
                consumer_group,
            )
            return
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                return
            raise
        except (ConnectionError, OSError):
            logger.warning(
                "coordinator listener: redis not ready (attempt %d/30)",
                attempt + 1,
            )
            await asyncio.sleep(1)
    logger.error("coordinator listener: redis not ready after 30s, group create skipped")


async def _handle_message(
    db_pool: Any,
    msg_id: str,
    data: dict,
) -> bool:
    """단일 메시지 처리. ACK 가능 여부 (True=성공) 반환.

    payload 가 비어 있거나 validate / INSERT 실패면 False 반환 → 호출부가 ACK
    안 함 → pending 에 남음. delivery_count 가 ``_DLQ_MAX_DELIVERIES`` 초과
    하면 ``_move_to_dlq`` 가 ACK + DLQ 이관.

    idempotency: stream_msg_id 가 UNIQUE 라 재배달 시 INSERT 가 ON CONFLICT
    DO NOTHING. 즉 retry 가 일어나도 event_log row 중복 X.
    """
    payload = data.get(b"payload") or data.get("payload")
    if not payload:
        logger.warning("coordinator listener: empty payload id=%s", msg_id)
        return False

    if isinstance(payload, bytes):
        payload = payload.decode()

    try:
        event = _EVENT_ADAPTER.validate_json(payload)
    except ValidationError:
        logger.exception("coordinator listener: validate failed id=%s", msg_id)
        return False

    subject_id = _subject_id_for(event)
    ticker = getattr(event, "ticker", None)
    decision_id = getattr(event, "decision_id", None)
    correlation_id = event.correlation_id
    occurred_at = event.occurred_at

    # payload_json: pydantic 의 model_dump_json (string) → asyncpg jsonb 캐스트.
    # mode="json" dict 가 아니라 string 으로 보내는 이유: asyncpg 가 dict 를
    # 직접 jsonb 로 넘기려면 codec 설정이 필요한데, ::jsonb 캐스트 + JSON
    # 문자열이 가장 안정적 (기존 코드베이스도 동일 패턴).
    payload_json_str = event.model_dump_json()

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                _INSERT_EVENT_LOG,
                event.event_kind,
                subject_id,
                ticker,
                payload_json_str,
                correlation_id,
                decision_id,
                msg_id,
                occurred_at,
            )
    except Exception:
        logger.exception(
            "coordinator listener: insert failed kind=%s id=%s",
            event.event_kind,
            msg_id,
        )
        return False

    logger.debug(
        "coordinator listener: stored kind=%s subject=%s id=%s",
        event.event_kind,
        subject_id,
        msg_id,
    )
    return True


async def _move_to_dlq(
    redis: aioredis.Redis,
    stream_key: str,
    consumer_group: str,
    msg_id: str,
    data: dict,
) -> None:
    """delivery 임계 초과 메시지를 DLQ stream 으로 이관 후 원 stream 에서 ACK.

    DLQ stream key 는 ``<stream_key>:dlq``. payload 와 함께 원본 msg_id 를
    metadata 로 남겨서 사후 분석/재처리 가능.
    """
    dlq_key = f"{stream_key}{_DLQ_SUFFIX}"
    payload = data.get(b"payload") or data.get("payload") or b""
    if isinstance(payload, bytes):
        payload = payload.decode()
    try:
        await redis.xadd(
            dlq_key,
            {"payload": payload, "original_msg_id": msg_id, "original_stream": stream_key},
            maxlen=_DLQ_MAXLEN,
            approximate=True,
        )
        await redis.xack(stream_key, consumer_group, msg_id)
        logger.warning(
            "coordinator listener: moved to DLQ id=%s stream=%s",
            msg_id,
            dlq_key,
        )
    except Exception:
        logger.exception("coordinator listener: DLQ move failed id=%s", msg_id)


async def _quarantine_stuck_pending(
    redis: aioredis.Redis,
    stream_key: str,
    consumer_group: str,
) -> None:
    """delivery_count >= _DLQ_MAX_DELIVERIES 인 pending 메시지를 DLQ 로 격리.

    XPENDING_RANGE 로 pending summary 조회 → delivery_count 초과 메시지 →
    XRANGE 로 본문 가져와 _move_to_dlq. listener 가 transient 실패로 ACK 못
    한 메시지가 무한 재시도 되지 않도록 차단.
    """
    try:
        # XPENDING <stream> <group> IDLE 0 - + 100
        pending = await redis.xpending_range(
            stream_key,
            consumer_group,
            min="-",
            max="+",
            count=100,
        )
    except Exception:
        logger.exception("coordinator listener: xpending_range failed")
        return

    for entry in pending or []:
        # entry keys: message_id (bytes), consumer (bytes), time_since_delivered (int),
        # times_delivered (int)
        delivered = int(entry.get("times_delivered", 0))
        if delivered < _DLQ_MAX_DELIVERIES:
            continue
        raw_id = entry.get("message_id")
        if raw_id is None:
            continue
        mid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        try:
            entries = await redis.xrange(stream_key, min=mid, max=mid, count=1)
        except Exception:
            logger.exception("coordinator listener: xrange failed for id=%s", mid)
            continue
        if not entries:
            # 이미 사라진 메시지 — ACK 만
            try:
                await redis.xack(stream_key, consumer_group, mid)
            except Exception:
                logger.exception("coordinator listener: xack(missing) failed id=%s", mid)
            continue
        _id, data = entries[0]
        await _move_to_dlq(redis, stream_key, consumer_group, mid, data)


async def run_listener(
    redis: aioredis.Redis,
    db_pool: Any,
    *,
    stop_event: asyncio.Event | None = None,
    stream_key: str = COORDINATOR_STREAM,
    consumer_group: str = COORDINATOR_GROUP,
    consumer_name: str | None = None,
) -> None:
    """``coordinator:events`` 를 구독해서 ``event_log`` 에 archive.

    Args:
        redis: 비동기 Redis client.
        db_pool: asyncpg.Pool. ``acquire()`` async context 제공해야 함.
        stop_event: graceful shutdown 신호. set 되면 다음 폴링 사이클에 종료.
        stream_key: 구독 stream key. 기본은 운영 ``coordinator:events``. 테스트
            에선 임시 key 로 override.
        consumer_group: consumer group 명. 기본 ``coordinator_listener``.
        consumer_name: consumer name. 미지정 시 env ``COORDINATOR_CONSUMER_NAME``
            우선, fallback hostname.

    Stage 1 동작:
      - consumer group 보장 (없으면 MKSTREAM 으로 생성)
      - XREADGROUP block=5s, batch=32
      - 각 메시지: payload JSON → CoordinatorEvent validate → event_log INSERT
        → ACK. validate/INSERT 실패는 ACK 안 함 → pending 잔존 → 재배달.
      - idempotency: stream_msg_id UNIQUE 라 재배달 시 ON CONFLICT DO NOTHING.
      - DLQ: delivery_count >= _DLQ_MAX_DELIVERIES 면 별도 stream 으로 격리
        후 ACK. 주기 _PENDING_CHECK_INTERVAL 사이클마다 검사.
    """
    await _ensure_group(redis, stream_key, consumer_group)
    consumer = consumer_name or _resolve_consumer_name()
    logger.info(
        "coordinator listener started: stream=%s group=%s consumer=%s",
        stream_key,
        consumer_group,
        consumer,
    )

    cycle_counter = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("coordinator listener: stop_event set, shutting down")
            return

        cycle_counter += 1
        if cycle_counter >= _PENDING_CHECK_INTERVAL:
            cycle_counter = 0
            await _quarantine_stuck_pending(redis, stream_key, consumer_group)

        try:
            messages = await redis.xreadgroup(
                consumer_group,
                consumer,
                {stream_key: ">"},
                count=_BATCH_SIZE,
                block=_BLOCK_MS,
            )
        except asyncio.CancelledError:
            logger.info("coordinator listener: cancelled")
            raise
        except aioredis.ConnectionError:
            logger.error(
                "coordinator listener: redis connection lost, retry in %ds",
                _RECONNECT_BACKOFF_SEC,
            )
            await asyncio.sleep(_RECONNECT_BACKOFF_SEC)
            continue
        except Exception:
            logger.exception("coordinator listener: xreadgroup error")
            await asyncio.sleep(1)
            continue

        if not messages:
            continue

        ack_ids: list[str] = []
        for _stream_name, entries in messages:
            for raw_id, data in entries:
                mid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
                try:
                    ok = await _handle_message(db_pool, mid, data)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("coordinator listener: handler crashed id=%s", mid)
                    ok = False
                if ok:
                    ack_ids.append(mid)

        if ack_ids:
            try:
                await redis.xack(stream_key, consumer_group, *ack_ids)
            except Exception:
                # ACK 실패해도 event_log 의 stream_msg_id UNIQUE 가 idempotency
                # 보장. 다음 polling 사이클에서 다시 처리되더라도 ON CONFLICT DO
                # NOTHING.
                logger.exception("coordinator listener: xack failed count=%d", len(ack_ids))


__all__ = [
    "COORDINATOR_CONSUMER_ENV",
    "COORDINATOR_GROUP",
    "run_listener",
]
