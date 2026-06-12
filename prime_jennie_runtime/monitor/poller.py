"""LivePositionsPoller — KIS Gateway `/balance` 주기 polling → Redis 스냅샷.

v2 원본: `prime_jennie/services/monitor/app.py` `_publish_live_snapshot` + tick consumer.
v3 는 fast_loop/tick_loop 이 실시간 매도 판정을 담당하므로, monitor 는 대시보드용
스냅샷 + metrics 에 집중.

polling 주기는 장 시간 인식 — 정규장/동시호가엔 ``interval_sec``(기본 30s), 그 외엔
``idle_interval_sec``(기본 300s). 잔고의 보유수량·예수금은 체결 때만 바뀌지만
평가금액·평가손익은 현재가에 연동돼 장중 계속 변하므로, 장중만 촘촘히 읽는다.

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
from prime_jennie_runtime.infra.balance_snapshot import LIVE_POSITIONS_KEY
from prime_jennie_runtime.kis_gateway.market_hours import MarketCalendar

logger = logging.getLogger(__name__)

MONITOR_STATUS_KEY = "monitoring:price_monitor"
HEARTBEAT_KEY = "monitor:heartbeat"
HEARTBEAT_TTL_SEC = 180  # 하한 — 실제 TTL 은 현재 polling 주기에 맞춰 늘어남

# 알림 임계 (consecutive failures). 장중 30s × 3 = 1.5분 — 시스템 자체 hiccup 은
# 가볍게 흡수하면서 KIS Gateway 장애는 빠르게 알림.
ALERT_FAILURE_THRESHOLD = 3

# 장 시간 외 polling 주기 (초). 평가금액이 안 변하므로 크게 늘려 KIS 호출을 절약.
IDLE_INTERVAL_SEC = 300.0

# gateway 응답 대기 한도. gateway 의 잔고 경로는 KIS 원장 거부 시 1+2+4초
# backoff 재시도 후 stale 캐시로 200 을 주는데, 그 체인이 최대 ~12초 걸린다.
# 5초였을 땐 stale 로 멀쩡히 응답될 폴을 시간 초과로 "gateway_unreachable" 라
# 오인해 거짓 critical 이 났다 (2026-06-12 13:46·14:17 — 분봉 수집과 잔고
# 폴이 겹친 구간). 체인 전체를 기다리도록 15초.
_GATEWAY_TIMEOUT_SEC = 15.0


class LivePositionsPoller:
    """주기적으로 KIS Gateway `/balance` 를 읽어 Redis 에 스냅샷을 쓴다.

    Parameters
    ----------
    redis_client:
        async redis 클라이언트.
    gateway_url:
        KIS Gateway base URL (예: `http://kis-gateway:8080`).
    interval_sec:
        장중(정규장/동시호가) polling 주기.
    idle_interval_sec:
        장 시간 외 polling 주기. 평가금액이 변하지 않아 크게 잡는다.
    client:
        httpx AsyncClient — 테스트에서 주입. None 이면 내부에서 생성.
    notifier:
        선택. 연속 실패 임계 도달 / 회복 시 GenericAlertNotification 발행.
        None 이면 알림 비활성 (로깅만).
    calendar:
        장 시간 판정용 MarketCalendar — 테스트에서 set_clock 주입. None 이면 생성.
    """

    def __init__(
        self,
        *,
        redis_client: aioredis.Redis,
        gateway_url: str,
        interval_sec: float = 30.0,
        idle_interval_sec: float = IDLE_INTERVAL_SEC,
        client: httpx.AsyncClient | None = None,
        notifier: Notifier | None = None,
        alert_threshold: int = ALERT_FAILURE_THRESHOLD,
        calendar: MarketCalendar | None = None,
    ) -> None:
        self._redis = redis_client
        self._gateway_url = gateway_url.rstrip("/")
        self._interval = interval_sec
        # idle 주기는 최소 interval_sec 이상으로 강제 (잘못된 설정 방어).
        self._idle_interval = max(interval_sec, idle_interval_sec)
        self._calendar = calendar or MarketCalendar()
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
            self._client = httpx.AsyncClient(timeout=_GATEWAY_TIMEOUT_SEC)
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
                await asyncio.wait_for(self._stop.wait(), timeout=self._current_interval())
            except TimeoutError:
                continue

    def _current_interval(self) -> float:
        """장중(정규장/동시호가/마감동시호가)이면 interval_sec, 그 외엔 idle_interval_sec.

        잔고 응답의 보유수량·예수금은 체결 시에만 바뀌지만 평가금액·평가손익은
        현재가에 연동돼 장중 계속 변한다 → 장중만 촘촘히 polling. 캘린더 오류 시엔
        보수적으로 fast(interval_sec) 로 폴백한다.
        """
        try:
            is_open, _ = self._calendar.is_market_open()
        except Exception:
            logger.debug("market hours check failed — fast interval 폴백", exc_info=True)
            return self._interval
        return self._interval if is_open else self._idle_interval

    # ------------------------------------------------------------------ one tick

    async def tick_once(self) -> int:
        """1회 polling. 성공 시 positions 개수를 반환, 실패 시 0."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_GATEWAY_TIMEOUT_SEC)
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
            # 스냅샷 TTL 은 polling 주기보다 충분히 길게 — 장외 느린 주기 (300s) 에도
            # 구독자 (dashboard/telegram/briefing 등, infra/balance_snapshot.py) 가
            # 스냅샷을 잃지 않도록. 이 스냅샷이 잔고의 단일 발행 지점이다.
            snapshot_ttl = int(self._current_interval()) + 120
            await self._redis.setex(LIVE_POSITIONS_KEY, snapshot_ttl, json.dumps(snapshot))
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
        if self.consecutive_failures >= self._alert_threshold and not self._alert_firing:
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
        # TTL 은 현재 polling 주기보다 충분히 길게 — 장 외 느린 주기에도 heartbeat 가
        # 만료되지 않도록 (consumer 가 정상 동작을 stale 로 오판하지 않게).
        ttl = max(HEARTBEAT_TTL_SEC, int(self._current_interval()) + 60)
        try:
            await self._redis.setex(HEARTBEAT_KEY, ttl, json.dumps(payload))
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
