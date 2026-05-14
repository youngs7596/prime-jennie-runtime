"""CoordinatorWatchPoller — Coordinator listener daemon 건강 감시.

Stage 1 의 listener daemon 은 read-only archive 라 실패해도 매매 path 에
영향이 없다. silent failure 가 silent 하지 않게 하기 위한 외부 watch.

watch 두 종류:
  - DLQ growth — ``coordinator:events:dlq`` XLEN 이 증가하면 critical alert.
    매 tick 마다 last_xlen 과 비교해 delta > 0 인 경우에만 발화 (폭주 시
    1회만). 첫 tick 에 baseline > 0 이면 warning 으로 1회 보고.
  - listener heartbeat staleness — ``heartbeat:coordinator-listener`` 키가
    없거나 갱신이 끊긴 상태가 연속 N tick 지속되면 critical alert.
    회복 시 info recovery alert.

연동:
  - GenericAlertNotification 을 Notifier 로 emit → telegram_bot 가 동일
    stream(v3:notifications) 에서 소비. monitor/poller.py 의 LivePositionsPoller
    와 정확히 같은 패턴.

자체 heartbeat:
  - ``monitor:coordinator_watch_heartbeat`` 에 short TTL 로 상태 노출.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from prime_jennie_runtime.coordinator.listener import (
    COORDINATOR_GROUP,  # noqa: F401  (의도적 import — 같은 contract 임을 명시)
)
from prime_jennie_runtime.coordinator.publish import COORDINATOR_STREAM
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification

logger = logging.getLogger(__name__)

DLQ_STREAM_KEY = f"{COORDINATOR_STREAM}:dlq"
LISTENER_HEARTBEAT_KEY = "heartbeat:coordinator-listener"
WATCH_HEARTBEAT_KEY = "monitor:coordinator_watch_heartbeat"
WATCH_HEARTBEAT_TTL_SEC = 180  # poll interval(30s) 보다 충분히 큼

# heartbeat 가 N tick 연속 missing 이면 alert. 30s × 5 = 2.5분.
# listener 는 30s 주기 갱신 + TTL 90s 이므로 진짜 dead 가 아니면 1~2 tick 안에
# 회복됨. 5 는 한 두 cycle 의 hiccup 은 흡수하면서 진짜 죽음을 빠르게 잡는 균형.
HEARTBEAT_FAILURE_THRESHOLD = 5


class CoordinatorWatchPoller:
    """Coordinator listener daemon 의 외부 health watch.

    Parameters
    ----------
    redis_client:
        async redis. listener daemon 과 같은 instance.
    interval_sec:
        polling 주기 (기본 30s).
    notifier:
        선택. None 이면 알림 비활성 (로깅만).
    heartbeat_threshold:
        연속 stale tick 개수 — 임계 도달 시 alert.
    """

    def __init__(
        self,
        *,
        redis_client: aioredis.Redis,
        interval_sec: float = 30.0,
        notifier: Notifier | None = None,
        heartbeat_threshold: int = HEARTBEAT_FAILURE_THRESHOLD,
        dlq_stream_key: str = DLQ_STREAM_KEY,
        listener_heartbeat_key: str = LISTENER_HEARTBEAT_KEY,
    ) -> None:
        self._redis = redis_client
        self._interval = interval_sec
        self._notifier = notifier
        self._heartbeat_threshold = max(1, heartbeat_threshold)
        self._dlq_key = dlq_stream_key
        self._listener_hb_key = listener_heartbeat_key

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

        # DLQ state — 첫 tick 은 baseline 만 잡는다 (None 이면 미초기화).
        self._last_dlq_xlen: int | None = None

        # heartbeat state
        self._heartbeat_consecutive_failures: int = 0
        self._heartbeat_alert_firing: bool = False

        # tick metadata
        self.last_tick_ts: float | None = None
        self.last_dlq_xlen_observed: int = 0
        self.last_listener_heartbeat_ok: bool = False

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="coordinator-watch-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._interval + 2.0)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("coordinator watch tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ one tick

    async def tick_once(self) -> None:
        """1회 polling — DLQ growth + listener heartbeat staleness 검사."""

        self.last_tick_ts = time.time()
        await self._check_dlq()
        await self._check_listener_heartbeat()
        await self._write_self_heartbeat()

    async def _check_dlq(self) -> None:
        try:
            cur = int(await self._redis.xlen(self._dlq_key))
        except Exception:
            # stream key 가 아직 없으면 XLEN 이 0 을 반환하지만, redis 자체
            # 장애 시 예외. 일단 0 으로 간주하고 진행 — heartbeat 쪽 check 가
            # redis 장애를 따로 잡는다.
            logger.debug("dlq xlen check failed", exc_info=True)
            cur = 0
        self.last_dlq_xlen_observed = cur

        if self._last_dlq_xlen is None:
            # 첫 tick — baseline 잡기. 기존에 row 가 있다면 warning 1회.
            self._last_dlq_xlen = cur
            if cur > 0:
                await self._emit_alert(
                    severity="warning",
                    title="Coordinator DLQ 비어있지 않음",
                    body=(
                        f"watch 시작 시점에 DLQ stream({self._dlq_key}) 에 이미 "
                        f"{cur} row 가 있습니다. 과거 실패 흔적인지 확인 필요."
                    ),
                    metadata={"dlq_xlen": cur, "phase": "baseline"},
                )
            return

        if cur > self._last_dlq_xlen:
            delta = cur - self._last_dlq_xlen
            await self._emit_alert(
                severity="critical",
                title="Coordinator DLQ row 증가",
                body=(
                    f"DLQ stream({self._dlq_key}) row 가 "
                    f"{self._last_dlq_xlen} → {cur} (+{delta}) 으로 증가했습니다. "
                    f"listener 가 처리 못 한 메시지가 격리되었습니다."
                ),
                metadata={
                    "dlq_xlen_prev": self._last_dlq_xlen,
                    "dlq_xlen_cur": cur,
                    "delta": delta,
                },
            )
        # cur == last_dlq_xlen 이거나, cur < last (XTRIM 또는 수동 정리) 면 무알림.
        self._last_dlq_xlen = cur

    async def _check_listener_heartbeat(self) -> None:
        try:
            raw = await self._redis.get(self._listener_hb_key)
        except Exception:
            logger.debug("listener heartbeat read failed", exc_info=True)
            raw = None

        ok = raw is not None
        self.last_listener_heartbeat_ok = ok

        if ok:
            was_firing = self._heartbeat_alert_firing
            self._heartbeat_consecutive_failures = 0
            if was_firing:
                await self._emit_alert(
                    severity="info",
                    title="Coordinator listener heartbeat 회복",
                    body=(
                        f"listener heartbeat key({self._listener_hb_key}) 가 다시 "
                        f"갱신되고 있습니다."
                    ),
                    metadata={"heartbeat_key": self._listener_hb_key},
                )
                self._heartbeat_alert_firing = False
            return

        self._heartbeat_consecutive_failures += 1
        if (
            self._heartbeat_consecutive_failures >= self._heartbeat_threshold
            and not self._heartbeat_alert_firing
        ):
            await self._emit_alert(
                severity="critical",
                title="Coordinator listener heartbeat 끊김",
                body=(
                    f"listener heartbeat key({self._listener_hb_key}) 가 "
                    f"{self._heartbeat_consecutive_failures} tick 연속 missing. "
                    f"coordinator-listener daemon 이 죽었거나 redis 와 연결이 끊겼을 "
                    f"수 있습니다."
                ),
                metadata={
                    "heartbeat_key": self._listener_hb_key,
                    "consecutive_failures": self._heartbeat_consecutive_failures,
                },
            )
            self._heartbeat_alert_firing = True

    async def _write_self_heartbeat(self) -> None:
        payload: dict[str, Any] = {
            "status": "online" if self.last_listener_heartbeat_ok else "listener_stale",
            "last_tick_ts": self.last_tick_ts,
            "last_dlq_xlen": self.last_dlq_xlen_observed,
            "heartbeat_consecutive_failures": self._heartbeat_consecutive_failures,
            "heartbeat_alert_firing": self._heartbeat_alert_firing,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            await self._redis.setex(
                WATCH_HEARTBEAT_KEY, WATCH_HEARTBEAT_TTL_SEC, json.dumps(payload)
            )
        except Exception:
            logger.debug("watch self-heartbeat write failed", exc_info=True)

    async def _emit_alert(
        self,
        *,
        severity: str,
        title: str,
        body: str,
        metadata: dict[str, Any],
    ) -> None:
        if self._notifier is None:
            logger.warning("coordinator-watch alert (no notifier): %s — %s", title, body)
            return
        try:
            await self._notifier.emit(
                GenericAlertNotification(
                    severity=severity,  # type: ignore[arg-type]
                    title=title,
                    body=body,
                    ts=datetime.now(UTC),
                    metadata={"component": "coordinator_watch", **metadata},
                )
            )
        except Exception:
            logger.exception("coordinator-watch alert emit failed")
