"""Slow Loop long-running runner (entrypoint for `slow-loop` 컨테이너).

`scheduled_jobs` (owner='slow_loop') 에 등록된 cron 을 apscheduler 가 트리거하면
`scout_daily` handler 가 `run_slow_loop()` 를 호출한다.

Role tier 매핑 (2026-04-22 재개정 — Opus 비용 부담으로 DeepSeek 복귀):
  - Scout (strong)    → DeepSeek chat (V3.2 flagship, DEEPSEEK_API_KEY)
  - Macro (reasoning) → DeepSeek chat (V3.2 flagship, DEEPSEEK_API_KEY)
  - Scout shadow      → Claude Opus 4.7 (회귀 검증용, ANTHROPIC_API_KEY)
  - Macro shadow      → Claude Opus 4.7 (회귀 검증용, ANTHROPIC_API_KEY)

배경: Macro shadow 4일치 (35 successful run) 비교에서 Gate 100% / Size 86%
일치 (불일치 4건 중 3건은 DeepSeek 가 더 보수적, 1건만 공격). 비용은 약 50×
저렴 ($0.003 vs $0.147/run). Scout 는 2026-04-19 이전엔 DeepSeek 으로 안정
운영. Opus shadow 로 1~2주 회귀 데이터 추가 수집 후 shadow 도 종료 가능.

Scout/Macro feeder 는 Phase 2 기준 Stub — Track E feeder 완성 시 교체 (AGENTS.md §위임 경계).

실행:
    python -m prime_jennie_runtime.slow_loop.app

환경:
    POSTGRES_* / REDIS_* — 공통
    DEEPSEEK_API_KEY — Scout 호출용
    ANTHROPIC_API_KEY — Macro 호출용
    (선택) DEEPSEEK_MODEL, DEEPSEEK_BASE_URL, ANTHROPIC_MODEL 으로 모델 override
    어느 하나 누락 시 스케줄은 기동하지만 handler 는 skip.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from minyoung_mah import (
    NullHITLChannel,
    NullMemoryStore,
    Orchestrator,
    RoleRegistry,
    TieredModelRouter,
    ToolRegistry,
    default_resilience,
)

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.infra.config import AppConfig, TelegramConfig, validate_kis_gateway_url
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.observer_impl import StructlogPJObserver
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner
from prime_jennie_runtime.infra.trading_calendar import is_trading_day_via_gateway
from prime_jennie_runtime.position_sheet.schema import KST

from .macro.context_builder import MacroContextBuilder
from .macro.feeders.stub import (
    StubKorMacroNewsFeeder,
    StubMarketSnapshotFeeder,
    StubWsjDigestFeeder,
)
from .macro.history_query import fetch_recent_macro_runs
from .macro.role import MacroGateRole
from .macro.state_store import MacroStateStore
from .macro.trigger_watcher import (
    DEFAULT_POLL_INTERVAL_SEC,
    TriggerWatcher,
    make_invoker,
)
from .pipeline import SlowLoopComponents, run_slow_loop
from .scout.context_builder import ScoutContextBuilder
from .scout.feeders.stub import (
    StubMarketSummaryFeeder,
    StubNewsEventFeeder,
    StubSectorMomentumFeeder,
    StubUniverseFeeder,
)
from .strategy.engine import StrategyEngine
from .strategy.policy import load_policy
from .strategy.publisher import PositionSheetPublisher
from .strategy.risk_throttle import RedisRiskThrottleSnapshot

OWNER = "slow_loop"

logger = logging.getLogger(__name__)


def _try_build_tiered_router() -> Any | None:
    """Role tier → ChatModel 매핑 반환. 필수 키 누락 시 None.

    tier 매핑 (2026-04-22 재개정):
      - strong    → Scout      = DeepSeek chat (DEEPSEEK_API_KEY)
      - reasoning → Macro Gate = DeepSeek chat (DEEPSEEK_API_KEY)
      - shadow_strong    → Scout shadow = Claude Opus 4.7 (ANTHROPIC_API_KEY)
      - shadow_reasoning → Macro shadow = Claude Opus 4.7 (ANTHROPIC_API_KEY)
      - fast      → 현재 slow_loop 에선 사용 X (news_pipeline 이 별도로 vLLM EXAONE 호출)

    DeepSeek 하나는 Scout + Macro 둘 다 같은 인스턴스로 재사용 (stateless API 라
    무해). Opus shadow 도 동일하게 단일 인스턴스 재사용.
    DeepSeek/Anthropic key 중 하나라도 없으면 slow_loop handler 는 skip.
    """
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not (deepseek_key and anthropic_key):
        logger.warning(
            "DEEPSEEK_API_KEY/ANTHROPIC_API_KEY 누락 — slow_loop LLM 비활성."
            " deepseek=%s anthropic=%s",
            bool(deepseek_key),
            bool(anthropic_key),
        )
        return None
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.warning(
            "langchain_openai/langchain_anthropic 미설치 — LLM 호출 비활성. extras [slow_loop] 필요"
        )
        return None

    deepseek_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")

    # DeepSeek chat: function_calling 만 지원 (json_schema response_format 미지원).
    class _DeepSeekChatOpenAI(ChatOpenAI):
        def with_structured_output(self, schema, *, method=None, **kwargs):  # type: ignore[override]
            return super().with_structured_output(
                schema, method=method or "function_calling", **kwargs
            )

    # Primary = DeepSeek chat (V3.2 flagship, Scout + Macro 둘 다 재사용)
    deepseek_chat = _DeepSeekChatOpenAI(
        model=deepseek_model,
        api_key=deepseek_key,
        base_url=deepseek_base,
        temperature=0.1,
    )
    strong = deepseek_chat
    reasoning = deepseek_chat

    # Shadow = Claude Opus 4.7 (회귀 검증용). temperature 파라미터 미지원 (API 400).
    opus_shadow = ChatAnthropic(
        model=anthropic_model,
        api_key=anthropic_key,
    )
    return {
        "strong": strong,
        "reasoning": reasoning,
        "default": strong,
        # shadow tier — primary orchestrator 와 동일 role 이 다른 모델로 병렬 평가
        "shadow_strong": opus_shadow,
        "shadow_reasoning": opus_shadow,
    }


def _build_observer(telegram_cfg: TelegramConfig) -> object:
    """Slow_loop observer — structlog only.

    (2026-05-29) scout codegen escalate hook 제거. ``pj.scout.escalate`` 를 emit 하던
    code_loop 이 결정론 코어로 은퇴해 텔레그램 escalate 의 emit 원천이 사라졌다.
    telegram_cfg 는 호출부 호환을 위해 시그니처에 남겨 둔다.
    """
    return StructlogPJObserver()


def _build_slow_loop_components(
    redis_client: aioredis.Redis,
    db_engine: Any = None,
    telegram_cfg: TelegramConfig | None = None,
) -> SlowLoopComponents | None:
    """SlowLoopComponents 조립. tier 라우터 구성 실패 시 None.

    db_engine 이 주어지면 macro_runs/scout_runs 를 persist. None 이면 기록 없이 실행.
    telegram_cfg 는 observer 합성에 전달되나 현재 escalate hook 은 비활성 (structlog only).
    """
    tiers = _try_build_tiered_router()
    if tiers is None:
        return None

    observer = _build_observer(telegram_cfg or TelegramConfig())
    orchestrator = Orchestrator(
        role_registry=RoleRegistry.of(MacroGateRole()),
        tool_registry=ToolRegistry(),
        model_router=TieredModelRouter(tiers),
        memory=NullMemoryStore(),
        hitl=NullHITLChannel(),
        observer=observer,
        resilience=default_resilience(),
    )

    # Shadow orchestrator — Macro gate 만 DeepSeek 로 병렬 평가. Opus 결정이 primary.
    # MACRO_SHADOW_ENABLED=0 으로 비활성화. (scout shadow 는 2026-05-22 결정론 코어
    # 전환으로 은퇴 — codegen 이 없으면 비교 대상이 무의미.)
    shadow_orchestrator: Orchestrator | None = None
    macro_shadow_on = os.environ.get("MACRO_SHADOW_ENABLED", "1") == "1"
    if macro_shadow_on and "shadow_reasoning" in tiers:
        shadow_tiers = {
            # macro gate 만 shadow 평가 — strong/default 는 fallback 안전치
            "strong": tiers["strong"],
            "reasoning": tiers["shadow_reasoning"],
            "default": tiers["default"],
        }
        shadow_orchestrator = Orchestrator(
            role_registry=RoleRegistry.of(MacroGateRole()),
            tool_registry=ToolRegistry(),
            model_router=TieredModelRouter(shadow_tiers),
            memory=NullMemoryStore(),
            hitl=NullHITLChannel(),
            observer=observer,
            resilience=default_resilience(),
        )

    # Real feeders (Phase 2.12 Track D). DB engine 없을 때는 stub 으로 fallback.
    if db_engine is not None:
        from prime_jennie_runtime.slow_loop.scout.feeders.real import (
            RealMarketSummaryFeeder,
            RealNewsEventFeeder,
            RealSectorMomentumFeeder,
            RealUniverseFeeder,
        )

        scout_builder = ScoutContextBuilder(
            universe=RealUniverseFeeder(engine=db_engine),
            news=RealNewsEventFeeder(engine=db_engine),
            sector=RealSectorMomentumFeeder(engine=db_engine),
            market=RealMarketSummaryFeeder(engine=db_engine),
            engine=db_engine,  # audit B1 — today_entries / recent_stops 조회용
        )
    else:
        scout_builder = ScoutContextBuilder(
            universe=StubUniverseFeeder(),
            news=StubNewsEventFeeder(),
            sector=StubSectorMomentumFeeder(),
            market=StubMarketSummaryFeeder(),
        )

    if db_engine is not None:
        from prime_jennie_runtime.slow_loop.macro.feeders.real import (
            RealKorMacroNewsFeeder,
            RealMarketSnapshotFeeder,
            RealWsjDigestFeeder,
        )

        macro_builder = MacroContextBuilder(
            wsj=RealWsjDigestFeeder(engine=db_engine, redis_client=redis_client),
            market=RealMarketSnapshotFeeder(engine=db_engine, redis_client=redis_client),
            kor=RealKorMacroNewsFeeder(engine=db_engine),
        )
    else:
        macro_builder = MacroContextBuilder(
            wsj=StubWsjDigestFeeder(),
            market=StubMarketSnapshotFeeder(),
            kor=StubKorMacroNewsFeeder(),
        )

    policy = load_policy()
    # fast_loop 가 Redis 에 적재한 intraday:risk:level 을 시트 발행 시 그대로 반영.
    risk_throttle = RedisRiskThrottleSnapshot(redis_client=redis_client)
    # audit B2 dead code 복구 — db_engine 있으면 실제 enforcement.
    # 미주입 시 NullActiveSheetChecker 로 fallback (legacy 동작).
    # 섹터 cap resolver (2026-05-22 step3) 도 동일 패턴 — db_engine 있으면 stock_masters
    # 기반 PgSectorResolver, 없으면 None → NullSectorResolver fallback (cap 비활성).
    if db_engine is not None:
        from prime_jennie_runtime.slow_loop.strategy.engine import (
            PgActiveSheetChecker,
            PgSectorResolver,
        )

        active_checker = PgActiveSheetChecker(db_engine)
        sector_resolver = PgSectorResolver(db_engine)
    else:
        active_checker = None  # StrategyEngine 이 NullActiveSheetChecker fallback
        sector_resolver = None  # StrategyEngine 이 NullSectorResolver fallback
    engine = StrategyEngine(
        policy=policy,
        risk_throttle=risk_throttle,
        active_checker=active_checker,
        sector_resolver=sector_resolver,
    )
    publisher = PositionSheetPublisher(client=redis_client, db_engine=db_engine)
    state_store = MacroStateStore(client=redis_client)
    system_state = SystemState(redis_client=redis_client)

    return SlowLoopComponents(
        orchestrator=orchestrator,
        scout_builder=scout_builder,
        macro_builder=macro_builder,
        engine=engine,
        publisher=publisher,
        state_store=state_store,
        observer=observer,
        db_engine=db_engine,
        shadow_orchestrator=shadow_orchestrator,
        redis_client=redis_client,
        system_state=system_state,
    )


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="slow-loop")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

        # DB engine 은 SchedulerStore + slow_loop persistence 양쪽이 공유
        engine = create_engine(cfg.postgres)
        stack.push_async_callback(engine.dispose)

        components = _build_slow_loop_components(
            redis_client, db_engine=engine, telegram_cfg=cfg.telegram
        )
        if components is None:
            logger.warning(
                "slow_loop components 미구성 (VLLM_LLM_URL/MODEL 또는 langchain_openai 누락). "
                "스케줄은 기동하지만 handler 는 skip"
            )

        kis_gateway_url = validate_kis_gateway_url(cfg.kis)

        async def scout_daily(trigger: str = "scheduled") -> None:
            if components is None:
                logger.warning("scout_daily skipped: components unavailable")
                return
            if not await is_trading_day_via_gateway(kis_gateway_url):
                logger.info("scout_daily skipped: non-trading day")
                return
            now = datetime.now(tz=KST)
            as_of_date = now.date()
            run_suffix = uuid.uuid4().hex[:8]
            macro_run_id = f"mr_{now:%Y%m%d_%H%M}_{run_suffix}"
            scout_run_id = f"sr_{now:%Y%m%d_%H%M}_{run_suffix}"
            logger.info(
                "slow_loop trigger=%s as_of=%s macro_run=%s scout_run=%s",
                trigger,
                as_of_date,
                macro_run_id,
                scout_run_id,
            )
            recent_macro_runs = (
                await fetch_recent_macro_runs(components.db_engine)
                if components.db_engine is not None
                else []
            )
            result = await run_slow_loop(
                components,
                as_of_date=as_of_date,
                as_of_dt=now,
                macro_run_id=macro_run_id,
                scout_run_id=scout_run_id,
                recent_macro_runs=recent_macro_runs,
                macro_trigger=f"scheduled:{trigger}",
                scout_trigger=f"scheduled:{trigger}",
            )
            logger.info(
                "slow_loop done: skipped=%s published=%d rejected=%d",
                result.skipped_reason,
                len(result.sheets_published),
                len(result.sheets_rejected),
            )

        handlers = {"scout_daily": scout_daily}

        scheduler = SchedulerRunner(
            owner=OWNER,
            handlers=handlers,
            store=PostgresSchedulerStore(engine),
            redis_client=redis_client,
            timezone_name=cfg.timezone,
        )
        await scheduler.start()
        stack.push_async_callback(scheduler.stop)

        # Macro 자동 ad-hoc 트리거 watcher (MACRO_GATE_SPEC §1.3).
        # MACRO_TRIGGER_WATCHER_ENABLED=0 으로 비활성. components 미구성 시 skip.
        watcher_task: asyncio.Task[int] | None = None
        if components is not None and os.environ.get("MACRO_TRIGGER_WATCHER_ENABLED", "1") == "1":
            interval = int(
                os.environ.get("MACRO_TRIGGER_WATCHER_INTERVAL_SEC", str(DEFAULT_POLL_INTERVAL_SEC))
            )
            watcher = TriggerWatcher(
                redis_client=redis_client,
                invoker=make_invoker(components, run_slow_loop_fn=run_slow_loop),
                observer=components.observer,
                interval_sec=interval,
            )
            watcher_task = asyncio.create_task(watcher.run(), name="macro_trigger_watcher")
            logger.info("macro trigger_watcher spawned (interval=%ds)", interval)

            async def _stop_watcher() -> None:
                if watcher_task is None or watcher_task.done():
                    return
                watcher_task.cancel()
                try:
                    await watcher_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

            stack.push_async_callback(_stop_watcher)
        else:
            logger.info(
                "macro trigger_watcher disabled (components=%s, env=%s)",
                components is not None,
                os.environ.get("MACRO_TRIGGER_WATCHER_ENABLED", "1"),
            )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        logger.info("slow_loop runner ready — waiting for signals")
        await stop_event.wait()
        logger.info("slow_loop runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
