"""v3 공용 스케줄러 — `scheduled_jobs` 테이블 기반 SchedulerRunner.

apscheduler 내장 pickle jobstore 대신, 평범한 Postgres 테이블(`scheduled_jobs`)을
진실 공급원으로 두고 control-ui 가 SQL 로 CRUD 할 수 있게 한다. 각 서비스(owner)
프로세스 안에서 `SchedulerRunner` 인스턴스가 자기 owner 의 job 만 로드해 apscheduler
`AsyncIOScheduler` 에 등록한다. 실행 전/후 훅이 실행 이력과 status 필드를 갱신한다.

Reload 는 N초 polling 으로 DB 상태와 현재 스케줄 집합을 비교해 차이만 반영한다.
즉시 반영이 필요하면 Redis pub/sub `scheduler.reload:{owner}` 로 트리거할 수 있다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

RELOAD_CHANNEL_PREFIX = "scheduler.reload"


# apscheduler 의 CronTrigger.from_crontab 는 day-of-week 를 cron 표준(0/7=Sun, 1=Mon)
# 대신 apscheduler 고유 규약(0=Mon ... 6=Sun) 으로 해석한다. 그대로 두면 "1-5" 가
# 월~금 대신 화~토로 실행되어 월요일 장중 스케줄이 전량 누락된다.
# DB 에는 cron 표준을 그대로 두고 여기서 dow 필드만 apscheduler 규약으로 치환한다.
_CRON_WEEKDAY_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def _cron_dow_to_apscheduler(token: str) -> str:
    """cron dow 단일 토큰을 apscheduler dow 로 변환. (0/7=Sun,1=Mon) → (0=Mon,6=Sun)."""

    token = token.strip()
    if not token or token in {"*", "?"}:
        return token
    lower = token.lower()
    if lower in _CRON_WEEKDAY_NAMES:
        n = _CRON_WEEKDAY_NAMES[lower]
    else:
        n = int(token)
        if n == 7:
            n = 0
        if not 0 <= n <= 6:
            raise ValueError(f"dow out of range: {token}")
    # cron n → apscheduler (n + 6) % 7
    return str((n + 6) % 7)


def _translate_cron_dow_field(field: str) -> str:
    """cron dow 필드 전체(`1-5`, `0,6`, `2/2` 등) 를 apscheduler 규약으로 치환."""

    pieces: list[str] = []
    for comma_piece in field.split(","):
        if "/" in comma_piece:
            base, step = comma_piece.split("/", 1)
            if "-" in base:
                a, b = base.split("-", 1)
                pieces.append(f"{_cron_dow_to_apscheduler(a)}-{_cron_dow_to_apscheduler(b)}/{step}")
            else:
                pieces.append(f"{_cron_dow_to_apscheduler(base)}/{step}")
        elif "-" in comma_piece:
            a, b = comma_piece.split("-", 1)
            pieces.append(f"{_cron_dow_to_apscheduler(a)}-{_cron_dow_to_apscheduler(b)}")
        else:
            pieces.append(_cron_dow_to_apscheduler(comma_piece))
    return ",".join(pieces)


def _normalize_cron_for_apscheduler(expr: str) -> str:
    """cron 표현식 전체의 dow 필드만 apscheduler 규약으로 치환한다."""

    parts = expr.split()
    if len(parts) != 5:
        return expr  # 5 필드가 아니면 apscheduler 가 알아서 ValueError 를 낸다
    parts[4] = _translate_cron_dow_field(parts[4])
    return " ".join(parts)


@dataclass(frozen=True)
class JobSpec:
    """scheduled_jobs 한 행의 스케줄러 관련 필드."""

    id: str
    handler_key: str
    cron: str
    kwargs: dict[str, Any]
    enabled: bool

    @property
    def signature(self) -> tuple:
        """변경 감지용 해시 키. cron/kwargs/handler_key/enabled 바뀌면 reschedule."""
        return (self.handler_key, self.cron, json.dumps(self.kwargs, sort_keys=True), self.enabled)


@runtime_checkable
class SchedulerStore(Protocol):
    """scheduler.py 가 DB 에 요구하는 최소 인터페이스.

    테스트에서는 fake 구현을, 운영에서는 `PostgresSchedulerStore` 를 주입.
    """

    async def fetch_specs(self, owner: str) -> list[JobSpec]: ...

    async def record_run_start(self, job_id: str, started_at: datetime) -> int: ...

    async def record_run_end(
        self,
        job_id: str,
        run_id: int,
        *,
        status: str,
        error: str | None,
        duration_ms: int,
        next_run_at: datetime | None,
    ) -> None: ...


class PostgresSchedulerStore:
    """SQLAlchemy AsyncEngine 기반 `SchedulerStore` 구현 (Postgres 16)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def fetch_specs(self, owner: str) -> list[JobSpec]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, handler_key, cron, kwargs, enabled "
                    "FROM scheduled_jobs WHERE owner = :owner"
                ),
                {"owner": owner},
            )
            rows = result.mappings().all()
        specs: list[JobSpec] = []
        for row in rows:
            kwargs_raw = row["kwargs"]
            if isinstance(kwargs_raw, str):
                kwargs = json.loads(kwargs_raw)
            else:
                kwargs = dict(kwargs_raw or {})
            specs.append(
                JobSpec(
                    id=row["id"],
                    handler_key=row["handler_key"],
                    cron=row["cron"],
                    kwargs=kwargs,
                    enabled=bool(row["enabled"]),
                )
            )
        return specs

    async def record_run_start(self, job_id: str, started_at: datetime) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO scheduled_job_runs (job_id, started_at, status) "
                    "VALUES (:job_id, :started_at, 'running') RETURNING run_id"
                ),
                {"job_id": job_id, "started_at": started_at},
            )
            run_id = int(result.scalar_one())
            await conn.execute(
                text(
                    "UPDATE scheduled_jobs SET last_run_at = :started_at, last_status = 'running', "
                    "last_error = NULL, updated_at = NOW() WHERE id = :job_id"
                ),
                {"started_at": started_at, "job_id": job_id},
            )
        return run_id

    async def record_run_end(
        self,
        job_id: str,
        run_id: int,
        *,
        status: str,
        error: str | None,
        duration_ms: int,
        next_run_at: datetime | None,
    ) -> None:
        finished_at = datetime.now(UTC)
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE scheduled_job_runs SET finished_at = :finished, status = :status, "
                    "error = :error, duration_ms = :duration WHERE run_id = :run_id"
                ),
                {
                    "finished": finished_at,
                    "status": status,
                    "error": error,
                    "duration": duration_ms,
                    "run_id": run_id,
                },
            )
            await conn.execute(
                text(
                    "UPDATE scheduled_jobs SET last_status = :status, last_error = :error, "
                    "last_duration_ms = :duration, next_run_at = :next_run, updated_at = NOW() "
                    "WHERE id = :job_id"
                ),
                {
                    "status": status,
                    "error": error,
                    "duration": duration_ms,
                    "next_run": next_run_at,
                    "job_id": job_id,
                },
            )


class SchedulerRunner:
    """owner 별 apscheduler 래퍼.

    Parameters
    ----------
    owner:
        scheduled_jobs.owner 와 일치하는 서비스 이름. 이 prefix 에 해당하는 job 만 적재.
    handlers:
        handler_key → async callable 매핑. kwargs 는 `**kwargs` 로 전달된다.
    store:
        `SchedulerStore` 구현. 운영은 `PostgresSchedulerStore(engine)`, 테스트는 fake.
    redis_client:
        reload pub/sub 구독용. None 이면 polling 만 사용.
    poll_interval_sec:
        DB 변경 감지 주기. 기본 30초.
    timezone_name:
        CronTrigger 에 적용할 타임존. 기본 Asia/Seoul.
    """

    def __init__(
        self,
        *,
        owner: str,
        handlers: dict[str, Handler],
        store: SchedulerStore,
        redis_client: aioredis.Redis | None = None,
        poll_interval_sec: float = 30.0,
        timezone_name: str = "Asia/Seoul",
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._owner = owner
        self._handlers = handlers
        self._store = store
        self._redis = redis_client
        self._poll_interval = poll_interval_sec
        self._timezone = timezone_name
        self._scheduler = scheduler or AsyncIOScheduler(timezone=timezone_name)
        self._known: dict[str, tuple] = {}  # job_id → signature
        self._poll_task: asyncio.Task[None] | None = None
        self._sub_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """첫 로드 + scheduler 기동 + reload 루프 시작."""
        await self.reload_once()
        self._scheduler.start()
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"scheduler-poll-{self._owner}"
        )
        if self._redis is not None:
            self._sub_task = asyncio.create_task(
                self._subscribe_reload(), name=f"scheduler-sub-{self._owner}"
            )
        logger.info(
            "scheduler started: owner=%s jobs=%d poll=%.1fs",
            self._owner,
            len(self._known),
            self._poll_interval,
        )

    async def stop(self) -> None:
        """graceful 종료. 진행 중인 job 은 마치도록 대기."""
        tasks = [t for t in (self._poll_task, self._sub_task) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._scheduler.shutdown(wait=True)
        logger.info("scheduler stopped: owner=%s", self._owner)

    # ------------------------------------------------------------------ reload

    async def reload_once(self) -> None:
        """DB 에서 spec 을 다시 읽어 scheduler 와 동기화. 테스트에서도 호출."""
        specs = await self._store.fetch_specs(self._owner)
        seen: set[str] = set()
        for spec in specs:
            seen.add(spec.id)
            sig = spec.signature
            if self._known.get(spec.id) == sig:
                continue
            if spec.id in self._known:
                try:
                    self._scheduler.remove_job(spec.id)
                except Exception:
                    logger.exception("remove_job failed id=%s", spec.id)
            if spec.enabled:
                self._add_job(spec)
            self._known[spec.id] = sig

        for gone in set(self._known) - seen:
            try:
                self._scheduler.remove_job(gone)
            except Exception:
                pass
            self._known.pop(gone, None)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self.reload_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler reload poll failed: owner=%s", self._owner)

    async def _subscribe_reload(self) -> None:
        assert self._redis is not None
        channel = f"{RELOAD_CHANNEL_PREFIX}:{self._owner}"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    await self.reload_once()
                except Exception:
                    logger.exception("scheduler pubsub reload failed: owner=%s", self._owner)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------ add / run

    def _add_job(self, spec: JobSpec) -> None:
        handler = self._handlers.get(spec.handler_key)
        if handler is None:
            logger.error(
                "scheduled job has unknown handler_key: id=%s handler_key=%s owner=%s",
                spec.id,
                spec.handler_key,
                self._owner,
            )
            return
        try:
            normalized = _normalize_cron_for_apscheduler(spec.cron)
            trigger = CronTrigger.from_crontab(normalized, timezone=self._timezone)
        except ValueError:
            logger.exception("invalid cron expression: id=%s cron=%r", spec.id, spec.cron)
            return
        self._scheduler.add_job(
            self.run_job,
            trigger=trigger,
            id=spec.id,
            kwargs={"job_id": spec.id, "handler": handler, "handler_kwargs": spec.kwargs},
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
        )

    async def run_job(
        self,
        job_id: str,
        handler: Handler,
        handler_kwargs: dict[str, Any],
    ) -> None:
        """job 1회 실행 + DB 상태 갱신. apscheduler 내부에서 호출되지만 테스트도 직접 호출."""
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        run_id = await self._store.record_run_start(job_id, started_at)
        try:
            await handler(**handler_kwargs)
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.exception("scheduled job failed: id=%s", job_id)
            await self._store.record_run_end(
                job_id,
                run_id,
                status="failed",
                error=f"{type(e).__name__}: {e}"[:500],
                duration_ms=duration_ms,
                next_run_at=self._next_run_for(job_id),
            )
            return
        duration_ms = int((time.monotonic() - t0) * 1000)
        await self._store.record_run_end(
            job_id,
            run_id,
            status="success",
            error=None,
            duration_ms=duration_ms,
            next_run_at=self._next_run_for(job_id),
        )

    def _next_run_for(self, job_id: str) -> datetime | None:
        try:
            job = self._scheduler.get_job(job_id)
        except Exception:
            return None
        return getattr(job, "next_run_time", None) if job is not None else None
