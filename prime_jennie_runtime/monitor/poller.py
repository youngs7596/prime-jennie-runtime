"""LivePositionsPoller — KIS Gateway `/balance` 주기 polling → Redis 스냅샷.

v2 원본: `prime_jennie/services/monitor/app.py` `_publish_live_snapshot` + tick consumer.
v3 는 fast_loop/tick_loop 이 실시간 매도 판정을 담당하므로, monitor 는 대시보드용
스냅샷 + metrics 에 집중.

Redis 키 (v2 와 동일):
- `monitoring:live_positions` — JSON {positions: [...], updated_at: iso}
- `monitoring:price_monitor`  — JSON {status, watching_count, updated_at}

추가 (Phase 2.10 #11 / v3 monitor heartbeat):
- `monitor:heartbeat` — JSON {status, last_success_ts, consecutive_failures}.
  최신 poll 상태를 short TTL 로 노출. monitoring:price_monitor 와 달리
  실패 시에도 갱신되어 외부에서 stale 판정 가능.
- 연속 실패 3회 도달 시 `GenericAlertNotification` 발행 (notifier 주입 시).
- 회복 (성공) 시 한 번 더 알림으로 해제 통지.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification

logger = logging.getLogger(__name__)

LIVE_POSITIONS_KEY = "monitoring:live_positions"
MONITOR_STATUS_KEY = "monitoring:price_monitor"
HEARTBEAT_KEY = "monitor:heartbeat"
HEARTBEAT_TTL_SEC = 180  # poll interval(30s) 보다 충분히 큼 → consumer 가 stale 감지

# 알림 임계 (consecutive failures). 30s × 3 = 1.5분 — 시스템 자체 hiccup 은
# 가볍게 흡수하면서 KIS Gateway 장애는 빠르게 알림.
ALERT_FAILURE_THRESHOLD = 3


class LivePositionsPoller:
    """주기적으로 KIS Gateway `/balance` 를 읽어 Redis 에 스냅샷을 쓴다.

    Parameters
    ----------
    redis_client:
        async redis 클라이언트.
    gateway_url:
        KIS Gateway base URL (예: `http://kis-gateway:8080`).
    interval_sec:
        polling 주기.
    client:
        httpx AsyncClient — 테스트에서 주입. None 이면 내부에서 생성.
    notifier:
        선택. 연속 실패 임계 도달 / 회복 시 GenericAlertNotification 발행.
        None 이면 알림 비활성 (로깅만).
    """

    def __init__(
        self,
        *,
        redis_client: aioredis.Redis,
        gateway_url: str,
        interval_sec: float = 30.0,
        client: httpx.AsyncClient | None = None,
        notifier: Notifier | None = None,
        alert_threshold: int = ALERT_FAILURE_THRESHOLD,
    ) -> None:
        self._redis = redis_client
        self._gateway_url = gateway_url.rstrip("/")
        self._interval = interval_sec
        self._client = client
        self._owned_client = client is None
        self._notifier = notifier
        self._alert_threshold = max(1, alert_threshold)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # 최근 성공/실패 타임스탬프 — metrics 에서 노출.
        self.last_success_ts: float | None = None
        self.last_failure_ts: float | None = None
        self.last_positions_count: int = 0
        # heartbeat / 알림 상태
        self.consecutive_failures: int = 0
        self._alert_firing: bool = False  # 임계 이상 알림 중이면 True, 회복 후 False

    async def __aenter__(self) -> LivePositionsPoller:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="live-positions-poller")

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
                logger.exception("live positions poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ one tick

    async def tick_once(self) -> int:
        """1회 polling. 성공 시 positions 개수를 반환, 실패 시 0."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
            self._owned_client = True
        try:
            resp = await self._client.get(f"{self._gateway_url}/api/balance")
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            await self._record_failure("gateway_unreachable")
            logger.warning("balance fetch failed", exc_info=True)
            return 0

        positions = payload.get("positions", []) or []
        snapshot = {
            "positions": positions,
            "updated_at": datetime.now(UTC).isoformat(),
            "cash_balance": payload.get("cash_balance"),
            "total_asset": payload.get("total_asset"),
            "stock_eval_amount": payload.get("stock_eval_amount"),
        }
        try:
            await self._redis.setex(LIVE_POSITIONS_KEY, 120, json.dumps(snapshot))
            await self._redis.setex(
                MONITOR_STATUS_KEY,
                60,
                json.dumps(
                    {
                        "status": "online",
                        "watching_count": len(positions),
                        "updated_at": snapshot["updated_at"],
                    }
                ),
            )
        except Exception:
            await self._record_failure("redis_write_failed")
            logger.warning("redis write failed", exc_info=True)
            return 0

        await self._record_success(len(positions))
        return len(positions)

    # ------------------------------------------------------------------ heartbeat

    async def _record_success(self, position_count: int) -> None:
        self.last_success_ts = time.time()
        self.last_positions_count = position_count
        was_failing = self.consecutive_failures >= self._alert_threshold
        self.consecutive_failures = 0
        if was_failing and self._alert_firing:
            await self._emit_alert(
                severity="info",
                title="Monitor recovered",
                body=(
                    f"KIS Gateway balance poll recovered after consecutive "
                    f"failures. position_count={position_count}"
                ),
            )
            self._alert_firing = False
        # heartbeat 는 _alert_firing 토글 후에 써야 alert_firing 필드가 일관성 있게 노출.
        await self._write_heartbeat(status="online", reason=None)

    async def _record_failure(self, reason: str) -> None:
        self.last_failure_ts = time.time()
        self.consecutive_failures += 1
        if (
            self.consecutive_failures >= self._alert_threshold
            and not self._alert_firing
        ):
            await self._emit_alert(
                severity="critical",
                title="Monitor poll failing",
                body=(
                    f"KIS Gateway balance poll failed "
                    f"{self.consecutive_failures} times in a row (reason={reason})."
                ),
            )
            self._alert_firing = True
        # heartbeat 는 _alert_firing 토글 후에 써서 firing 여부를 정확히 노출.
        await self._write_heartbeat(status="degraded", reason=reason)

    async def _write_heartbeat(self, *, status: str, reason: str | None) -> None:
        payload = {
            "status": status,
            "last_success_ts": self.last_success_ts,
            "last_failure_ts": self.last_failure_ts,
            "consecutive_failures": self.consecutive_failures,
            "alert_firing": self._alert_firing,
            "reason": reason,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            await self._redis.setex(HEARTBEAT_KEY, HEARTBEAT_TTL_SEC, json.dumps(payload))
        except Exception:
            logger.debug("heartbeat write failed", exc_info=True)

    async def _emit_alert(self, *, severity: str, title: str, body: str) -> None:
        if self._notifier is None:
            logger.warning("monitor alert (no notifier): %s — %s", title, body)
            return
        try:
            await self._notifier.emit(
                GenericAlertNotification(
                    severity=severity,  # type: ignore[arg-type]
                    title=title,
                    body=body,
                    ts=datetime.now(UTC),
                    metadata={
                        "component": "monitor",
                        "consecutive_failures": self.consecutive_failures,
                    },
                )
            )
        except Exception:
            logger.exception("monitor alert emit failed")
