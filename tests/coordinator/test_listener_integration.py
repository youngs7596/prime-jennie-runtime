"""Coordinator listener integration test — publish → listener → event_log DB row.

Redis + Postgres 가 실제로 있는 환경에서만 동작. CI / 로컬 unit 러닝에선 skip.

검증 시나리오:
  1. SheetPublishedEvent + EntryFilledEvent + ExitFilledEvent 3건 publish.
  2. listener 를 ``asyncio.create_task`` 로 띄움.
  3. listener 가 stream consume → event_log 에 3 row insert 할 때까지 polling.
  4. 각 row 의 payload_json 을 다시 Pydantic 으로 parse → 입력 이벤트와 동일성 확인.
  5. cleanup: listener stop_event set, stream + consumer group 제거,
     event_log 의 테스트 correlation_id row 삭제.

Stream key 처리:
  - 운영 stream key 는 hardcode 인 ``coordinator:events``. listener / publish 가
    parametrize 를 지원하지 않을 가능성이 있어, 가능한 한 import 해서 그 signature
    를 확인하고 임시 stream key 를 주입한다. 안 되면 운영 stream key 그대로 사용하고
    test 전후로 cleanup 으로 운영 stream 오염 방지.
  - 한 correlation_id prefix (``test-<uuid>``) 로 묶어 cleanup 시 정확히 그 row 만 지움.

환경변수 (있을 때만 실행):
  - REDIS_HOST / REDIS_PORT / REDIS_PASSWORD (또는 RedisConfig default)
  - POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
  - 추가로 ``COORDINATOR_INTEGRATION=1`` 명시되어야 실행 — 잘못 켜져서 운영 DB 건들지 않도록.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.coordinator.events import (
    EntryFilledEvent,
    ExitFilledEvent,
    SheetPublishedEvent,
)

# Stage 1.5 contract — publish/listener agent 와 합의된 고정값.
PROD_STREAM_KEY = "coordinator:events"
PROD_CONSUMER_GROUP = "coordinator_listener"


pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.environ.get("COORDINATOR_INTEGRATION", "").strip() in {"1", "true", "yes"}


def _skip_if_disabled() -> None:
    if not _integration_enabled():
        pytest.skip(
            "COORDINATOR_INTEGRATION env 가 설정 안 됨 — integration test skip (운영 DB/Redis 보호)"
        )


def _try_import_listener() -> tuple[Callable[..., Awaitable[None]], Callable[..., Awaitable[str]]]:
    """publish_event / run_listener 동적 import.

    아직 구현 중이라 ImportError 가능 — 그 경우 skip.
    """
    try:
        listener_mod = importlib.import_module("prime_jennie_runtime.coordinator.listener")
        publish_mod = importlib.import_module("prime_jennie_runtime.coordinator.publish")
    except ImportError as e:
        pytest.skip(f"coordinator listener/publish 모듈 미존재: {e}")

    if not hasattr(listener_mod, "run_listener"):
        pytest.skip("coordinator.listener.run_listener 미정의")
    if not hasattr(publish_mod, "publish_event"):
        pytest.skip("coordinator.publish.publish_event 미정의")

    return publish_mod.publish_event, listener_mod.run_listener


def _resolve_redis_url() -> str:
    """RedisConfig pattern 그대로 — env 가 없으면 default 사용."""
    from prime_jennie_runtime.infra.config import RedisConfig

    return RedisConfig().url


def _resolve_pg_dsn() -> str:
    """asyncpg 직접 사용용 DSN. PostgresConfig 의 dsn 은 SQLAlchemy URL 이라 분리."""
    user = os.environ.get("POSTGRES_USER", "pj_runtime")
    password = os.environ.get("POSTGRES_PASSWORD", "dev_password")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "prime_jennie_v3")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _redis_reachable(url: str) -> bool:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return False
    client = aioredis.from_url(url)
    try:
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


async def _pg_reachable(dsn: str) -> bool:
    try:
        import asyncpg
    except ImportError:
        return False
    try:
        conn = await asyncpg.connect(dsn)
        await conn.close()
        return True
    except Exception:
        return False


def _build_sample_events(correlation_id: str) -> list:
    now = datetime.now(UTC)
    return [
        SheetPublishedEvent(
            occurred_at=now,
            correlation_id=correlation_id,
            sheet_id=correlation_id,
            ticker="005930",
            strategy_tag="integration_test",
            final_pct=1.0,
            sheet_generated_at=now,
        ),
        EntryFilledEvent(
            occurred_at=now,
            correlation_id=correlation_id,
            sheet_id=correlation_id,
            ticker="005930",
            filled_qty=10,
            filled_price=70000.0,
            intended_quantity=10,
            is_partial=False,
        ),
        ExitFilledEvent(
            occurred_at=now,
            correlation_id=correlation_id,
            sheet_id=correlation_id,
            ticker="005930",
            filled_qty=10,
            filled_price=72000.0,
            entry_price=70000.0,
            reason="trailing_tp",
            fully_closed=True,
            pnl_amount=20000.0,
            pnl_pct=2.857,
        ),
    ]


def _listener_kwargs(run_listener: Callable, stop_event: asyncio.Event) -> dict:
    """run_listener 가 stream_key 등 parametrize 를 지원하면 임시 키 주입.

    안 하면 production stream key 그대로. 반환 dict 와 함께 어떤 stream/group 을
    써야 cleanup 할지도 결정해야 하므로 caller 가 metadata 도 받게 한다.
    """
    sig = inspect.signature(run_listener)
    kwargs: dict = {"stop_event": stop_event}
    if "stream_key" in sig.parameters:
        kwargs["stream_key"] = f"coordinator:events:test-{uuid.uuid4().hex[:8]}"
    if "consumer_group" in sig.parameters:
        kwargs["consumer_group"] = f"coordinator_listener_test_{uuid.uuid4().hex[:8]}"
    return kwargs


@pytest.mark.asyncio
async def test_publish_listener_event_log_roundtrip():
    """publish → listener → event_log 라운드트립.

    correlation_id 로 본 테스트의 row 만 정확히 식별. listener 가 한 cycle 돌고
    event_log 에 3 row 가 insert 될 때까지 polling (max 10s).
    """
    _skip_if_disabled()

    publish_event, run_listener = _try_import_listener()

    redis_url = _resolve_redis_url()
    pg_dsn = _resolve_pg_dsn()

    if not await _redis_reachable(redis_url):
        pytest.skip(f"Redis 연결 실패: {redis_url}")
    if not await _pg_reachable(pg_dsn):
        pytest.skip(f"Postgres 연결 실패: {pg_dsn}")

    import asyncpg
    import redis.asyncio as aioredis

    correlation_id = f"test-{uuid.uuid4().hex}"
    events = _build_sample_events(correlation_id)

    redis_client = aioredis.from_url(redis_url)
    pg_pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    assert pg_pool is not None  # mypy 안심 + 런타임 sanity

    stop_event = asyncio.Event()
    kwargs = _listener_kwargs(run_listener, stop_event)

    used_stream = kwargs.get("stream_key", PROD_STREAM_KEY)
    used_group = kwargs.get("consumer_group", PROD_CONSUMER_GROUP)

    # listener 의 publish 도 같은 stream key 를 향해야 한다. publish_event 가
    # stream_key kwarg 를 받으면 같이 넘긴다. 안 받으면 운영 stream 으로.
    pub_sig = inspect.signature(publish_event)
    pub_extra: dict = {}
    if "stream_key" in pub_sig.parameters and "stream_key" in kwargs:
        pub_extra["stream_key"] = kwargs["stream_key"]

    listener_task: asyncio.Task | None = None
    try:
        # listener 를 백그라운드로 띄운 뒤 publish — group 생성이 보장돼야
        # XREADGROUP 가 받을 수 있다. run_listener 가 group 생성을 내부에서 하므로
        # 짧게 await 만 양보해서 시작 후 publish.
        listener_task = asyncio.create_task(run_listener(redis_client, pg_pool, **kwargs))
        # listener startup grace — group 생성 시간.
        await asyncio.sleep(0.5)

        for ev in events:
            await publish_event(redis_client, ev, **pub_extra)

        # event_log 에 3 row 가 들어올 때까지 polling.
        deadline = asyncio.get_event_loop().time() + 10.0
        rows: list = []
        while asyncio.get_event_loop().time() < deadline:
            async with pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT event_kind, payload_json, correlation_id, occurred_at "
                    "FROM event_log WHERE correlation_id = $1 ORDER BY event_id ASC",
                    correlation_id,
                )
            if len(rows) >= 3:
                break
            await asyncio.sleep(0.2)

        assert len(rows) == 3, (
            f"event_log 에 3 row 가 기대됐는데 {len(rows)}개 — "
            f"listener 가 stream={used_stream} 을 정상 consume 못 했을 수 있음"
        )

        kinds_in_db = [r["event_kind"] for r in rows]
        assert kinds_in_db == ["sheet_published", "entry_filled", "exit_filled"]

        # payload round-trip — DB JSONB → Pydantic 다시 parse.
        import json

        for ev, row in zip(events, rows, strict=True):
            stored = row["payload_json"]
            if isinstance(stored, str):
                stored = json.loads(stored)
            # event 의 원본 dict 와 비교 (직렬화/역직렬화 round-trip 동일성).
            expected = json.loads(ev.model_dump_json())
            assert stored == expected, f"payload mismatch for {ev.event_kind}"
    finally:
        stop_event.set()
        if listener_task is not None:
            listener_task.cancel()
            try:
                await asyncio.wait_for(listener_task, timeout=3.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # cleanup — DB row.
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM event_log WHERE correlation_id = $1",
                    correlation_id,
                )
        except Exception:
            pass

        # cleanup — Redis stream + group. 운영 stream 인 경우 group 은 삭제하지
        # 않는다 (다른 consumer 영향). 임시 stream/group 일 때만 깨끗이 제거.
        try:
            if used_stream != PROD_STREAM_KEY:
                try:
                    await redis_client.xgroup_destroy(used_stream, used_group)
                except Exception:
                    pass
                await redis_client.delete(used_stream)
            else:
                # production stream — test correlation_id 의 entry 만 식별/삭제하긴
                # 어려우므로 (XADD 후 trim 필요), group pending 만 ACK 시도.
                try:
                    pending = await redis_client.xpending_range(
                        used_stream, used_group, "-", "+", count=100
                    )
                    for p in pending:
                        try:
                            await redis_client.xack(used_stream, used_group, p["message_id"])
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass

        await pg_pool.close()
        await redis_client.aclose()
