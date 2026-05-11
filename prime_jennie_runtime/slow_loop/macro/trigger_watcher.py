"""Macro Gate 자동 ad-hoc 트리거 watcher.

MACRO_GATE_SPEC §1.3 의 "자동 트리거" 구현. 정시 cron (08:00/09:30/10:30 등) 사이에
시장 급변이 발생하면 즉시 Macro pipeline 을 ad-hoc 실행하여 closed 판정 기회를 준다.

읽는 데이터 (Redis `macro:data:snapshot:{YYYY-MM-DD}` — jobs/council_macro 가 발행):
  - `kospi_change_pct` (percent, 예: -3.2)
  - `vix` (절대값, 예: 32.0)
  - `usd_krw` (절대값) — 전일 snapshot 의 동일 필드와 비교해 change_pct 산출

트리거 조건 (스펙 §1.3):
  - KOSPI 일중 하락률 <= -3.0%  → reason="ad_hoc_kospi_drop_3pct"
  - VIX 절대값 >= 30           → reason="ad_hoc_vix_spike_30"
  - |USD/KRW 일간 변동률| >= 1.5% → reason="ad_hoc_fx_shock_1p5pct"

debounce: 같은 reason 으로 30분 내 재트리거 방지 — Redis SET with EX 1800.
key: `macro:trigger_watcher:debounce:{reason}`.

트리거 시 동작:
  - 새 macro_run_id 생성 후 `run_slow_loop(comp, ..., macro_trigger=reason, ...)` 호출
  - 결과는 기존 cron path 와 동일 흐름으로 macro_runs DB 에 persist
  - 같은 reason 에 한해 debounce 동작 — 다른 reason 은 독립적으로 트리거 가능

폴링 주기: 기본 60 초 (MACRO_TRIGGER_WATCHER_INTERVAL_SEC env override 가능).
fail-open: 데이터 결손/Redis 에러는 swallow 후 다음 cycle 로.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.position_sheet.schema import KST

logger = logging.getLogger(__name__)

# 인보커: macro_trigger 문자열을 받아 run_slow_loop 를 실행.
SlowLoopInvoker = Callable[[str], Awaitable[None]]

MACRO_SNAPSHOT_KEY_PREFIX = "macro:data:snapshot:"
DEBOUNCE_KEY_PREFIX = "macro:trigger_watcher:debounce:"
DEBOUNCE_TTL_SEC = 1800  # 30 분 — 같은 reason 중복 트리거 방지

# 스펙 §1.3 임계값
KOSPI_DROP_THRESHOLD_PCT = -3.0  # percent, 일중 등락률
VIX_SPIKE_THRESHOLD = 30.0  # 절대값
FX_SHOCK_THRESHOLD_PCT = 1.5  # percent, |day_change|

# 폴링 주기 기본 60 초
DEFAULT_POLL_INTERVAL_SEC = 60
MAX_SNAPSHOT_LOOKBACK_DAYS = 5  # 주말/공휴일 (전일 USD/KRW lookup)

KOSPI_REASON = "ad_hoc_kospi_drop_3pct"
VIX_REASON = "ad_hoc_vix_spike_30"
FX_REASON = "ad_hoc_fx_shock_1p5pct"


@dataclass(frozen=True)
class TriggerSignal:
    """watcher 가 감지한 단일 트리거."""

    reason: str
    metric_name: str
    value: float
    threshold: float


# ----------------------------------------------------------------- helpers


async def _load_snapshot_for_date(
    redis_client: aioredis.Redis, target: date
) -> dict[str, Any] | None:
    key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{target.isoformat()}"
    raw = await redis_client.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw if isinstance(raw, (str, bytes, bytearray)) else str(raw))
    except json.JSONDecodeError:
        logger.warning("trigger_watcher: invalid JSON in snapshot key=%s", key)
        return None


async def _load_latest_snapshot(
    redis_client: aioredis.Redis, as_of: datetime
) -> tuple[dict[str, Any] | None, date | None]:
    """최근 며칠 (주말/공휴일 감안) 내 가장 최신 snapshot 을 찾는다."""
    target = as_of.date()
    for i in range(MAX_SNAPSHOT_LOOKBACK_DAYS):
        day = target - timedelta(days=i)
        snap = await _load_snapshot_for_date(redis_client, day)
        if snap is not None:
            return snap, day
    return None, None


async def _load_prior_snapshot(
    redis_client: aioredis.Redis, exclude_day: date
) -> dict[str, Any] | None:
    """`exclude_day` 바로 직전 영업일 snapshot. fx change_pct 산출용."""
    for i in range(1, MAX_SNAPSHOT_LOOKBACK_DAYS + 1):
        day = exclude_day - timedelta(days=i)
        snap = await _load_snapshot_for_date(redis_client, day)
        if snap is not None:
            return snap
    return None


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- evaluator


def evaluate_triggers(
    latest: dict[str, Any],
    prior: dict[str, Any] | None,
) -> list[TriggerSignal]:
    """스펙 §1.3 임계 룰 평가. 트리거된 신호 리스트 반환.

    snapshot 필드 단위:
      - `kospi_change_pct`: percent (예: -3.2)
      - `vix`: 절대값
      - `usd_krw`: 절대값 (전일 snapshot 의 동일 필드와 비교)
    """
    signals: list[TriggerSignal] = []

    kospi_pct = _as_float(latest.get("kospi_change_pct"))
    if kospi_pct is not None and kospi_pct <= KOSPI_DROP_THRESHOLD_PCT:
        signals.append(
            TriggerSignal(
                reason=KOSPI_REASON,
                metric_name="kospi_change_pct",
                value=kospi_pct,
                threshold=KOSPI_DROP_THRESHOLD_PCT,
            )
        )

    vix = _as_float(latest.get("vix"))
    if vix is not None and vix >= VIX_SPIKE_THRESHOLD:
        signals.append(
            TriggerSignal(
                reason=VIX_REASON,
                metric_name="vix",
                value=vix,
                threshold=VIX_SPIKE_THRESHOLD,
            )
        )

    # FX: 전일 snapshot 의 usd_krw 가 있을 때만 계산
    usd_krw = _as_float(latest.get("usd_krw"))
    usd_krw_prev = _as_float(prior.get("usd_krw") if prior else None)
    if usd_krw is not None and usd_krw_prev is not None and usd_krw_prev > 0:
        change_pct = (usd_krw / usd_krw_prev - 1.0) * 100.0
        if abs(change_pct) >= FX_SHOCK_THRESHOLD_PCT:
            signals.append(
                TriggerSignal(
                    reason=FX_REASON,
                    metric_name="usd_krw_change_pct",
                    value=change_pct,
                    threshold=FX_SHOCK_THRESHOLD_PCT,
                )
            )

    return signals


# ----------------------------------------------------------------- debounce


async def _try_acquire_debounce(
    redis_client: aioredis.Redis, reason: str, ttl_sec: int = DEBOUNCE_TTL_SEC
) -> bool:
    """`reason` 에 대해 debounce lock 획득. True 면 실행 권한, False 면 skip."""
    key = f"{DEBOUNCE_KEY_PREFIX}{reason}"
    # SET NX EX — 처음이면 True, 이미 있으면 None
    result = await redis_client.set(key, "1", nx=True, ex=ttl_sec)
    return bool(result)


# ----------------------------------------------------------------- runner


@dataclass
class TriggerWatcher:
    """Redis snapshot 폴링 → 임계 도달 시 SlowLoopInvoker 호출.

    `invoker` 는 `(macro_trigger: str) -> Awaitable[None]` 형태로, 보통 app.py 가
    `scout_daily` 와 유사한 클로저로 주입한다.
    """

    redis_client: aioredis.Redis
    invoker: SlowLoopInvoker
    observer: Any  # minyoung_mah.Observer — 옵션
    interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
    clock: Callable[[], datetime] | None = None
    debounce_ttl_sec: int = DEBOUNCE_TTL_SEC

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        return datetime.now(tz=KST)

    async def evaluate_once(self) -> list[TriggerSignal]:
        """1 회 폴링 — snapshot 로드 + 임계 평가. side-effect 없음 (디바운스/트리거 X)."""
        now = self._now()
        latest, latest_day = await _load_latest_snapshot(self.redis_client, now)
        if latest is None or latest_day is None:
            return []
        prior = await _load_prior_snapshot(self.redis_client, latest_day)
        return evaluate_triggers(latest, prior)

    async def process_once(self) -> list[TriggerSignal]:
        """1 cycle: 평가 + 디바운스 통과 시 invoker 호출. 실제 트리거된 신호 반환."""
        try:
            signals = await self.evaluate_once()
        except Exception:
            logger.exception("trigger_watcher: evaluate_once failed")
            return []

        fired: list[TriggerSignal] = []
        for sig in signals:
            try:
                acquired = await _try_acquire_debounce(
                    self.redis_client, sig.reason, self.debounce_ttl_sec
                )
            except Exception:
                logger.exception("trigger_watcher: debounce acquire failed reason=%s", sig.reason)
                continue
            if not acquired:
                logger.debug(
                    "trigger_watcher: debounced reason=%s (within %ds)",
                    sig.reason,
                    self.debounce_ttl_sec,
                )
                if self.observer is not None:
                    await self.observer.emit(
                        pj_event(
                            "pj.macro.trigger_debounced",
                            role="macro_gate",
                            ok=True,
                            reason=sig.reason,
                            metric=sig.metric_name,
                            value=sig.value,
                        )
                    )
                continue

            if self.observer is not None:
                await self.observer.emit(
                    pj_event(
                        "pj.macro.trigger_fired",
                        role="macro_gate",
                        ok=True,
                        reason=sig.reason,
                        metric=sig.metric_name,
                        value=sig.value,
                        threshold=sig.threshold,
                    )
                )
            logger.warning(
                "trigger_watcher: fired reason=%s metric=%s value=%.4f threshold=%.4f",
                sig.reason,
                sig.metric_name,
                sig.value,
                sig.threshold,
            )

            try:
                await self.invoker(sig.reason)
                fired.append(sig)
            except Exception:
                logger.exception(
                    "trigger_watcher: invoker raised reason=%s — debounce 유지 (수동 재시도 필요)",
                    sig.reason,
                )

        return fired

    async def run(self, max_iterations: int | None = None) -> int:
        """무한 루프. SIGTERM → CancelledError 로 종료. `max_iterations` 는 테스트용."""
        iterations = 0
        logger.info(
            "macro trigger_watcher start: interval=%ds debounce=%ds",
            self.interval_sec,
            self.debounce_ttl_sec,
        )
        try:
            while True:
                if max_iterations is not None and iterations >= max_iterations:
                    return iterations
                await self.process_once()
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    return iterations
                await asyncio.sleep(self.interval_sec)
        except asyncio.CancelledError:
            logger.info("macro trigger_watcher cancelled after %d iterations", iterations)
            raise


# ----------------------------------------------------------------- helper for app.py


def make_invoker(
    components: Any,
    *,
    run_slow_loop_fn: Callable[..., Awaitable[Any]],
    clock: Callable[[], datetime] | None = None,
) -> SlowLoopInvoker:
    """app.py 의 scout_daily 와 유사한 인보커를 빌드. macro_trigger 이름만 다르다.

    `run_slow_loop_fn` 은 보통 `prime_jennie_runtime.slow_loop.pipeline.run_slow_loop`
    이지만 테스트에선 mock 주입.
    """

    async def _invoker(macro_trigger: str) -> None:
        now = (clock or (lambda: datetime.now(tz=KST)))()
        as_of_date = now.date()
        run_suffix = uuid.uuid4().hex[:8]
        macro_run_id = f"mr_{now:%Y%m%d_%H%M}_{run_suffix}"
        scout_run_id = f"sr_{now:%Y%m%d_%H%M}_{run_suffix}"
        logger.info(
            "slow_loop ad-hoc trigger=%s as_of=%s macro_run=%s scout_run=%s",
            macro_trigger,
            as_of_date,
            macro_run_id,
            scout_run_id,
        )
        await run_slow_loop_fn(
            components,
            as_of_date=as_of_date,
            as_of_dt=now,
            macro_run_id=macro_run_id,
            scout_run_id=scout_run_id,
            macro_trigger=macro_trigger,
            scout_trigger=macro_trigger,
        )

    return _invoker


__all__ = [
    "DEBOUNCE_TTL_SEC",
    "FX_REASON",
    "FX_SHOCK_THRESHOLD_PCT",
    "KOSPI_DROP_THRESHOLD_PCT",
    "KOSPI_REASON",
    "TriggerSignal",
    "TriggerWatcher",
    "VIX_REASON",
    "VIX_SPIKE_THRESHOLD",
    "evaluate_triggers",
    "make_invoker",
]
