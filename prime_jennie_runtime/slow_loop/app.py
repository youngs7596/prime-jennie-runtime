"""Slow Loop long-running runner (entrypoint for `slow-loop` 컨테이너).

`scheduled_jobs` (owner='slow_loop') 에 등록된 cron 을 apscheduler 가 트리거하면
`scout_daily` handler 가 `run_slow_loop()` 를 호출한다.

실 LLM 은 vLLM EXAONE (OpenAI 호환) 을 `langchain_openai.ChatOpenAI` 로 감싸 주입.
Scout/Macro feeder 는 Phase 2 기준 Stub — Track E feeder 완성 시 교체 (AGENTS.md §위임 경계).

실행:
    python -m prime_jennie_runtime.slow_loop.app

환경:
    POSTGRES_* / REDIS_* — 공통
    VLLM_LLM_URL / VLLM_LLM_MODEL — chat model.
      미설정 시 LLM 없음 → 스케줄은 돌지만 handler 는 skip 로그 후 즉시 리턴.
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
    NullObserver,
    Orchestrator,
    RoleRegistry,
    SingleModelRouter,
    ToolRegistry,
    default_resilience,
)

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner
from prime_jennie_runtime.position_sheet.schema import KST

from .macro.context_builder import MacroContextBuilder
from .macro.feeders.stub import (
    StubKorMacroNewsFeeder,
    StubMarketSnapshotFeeder,
    StubWsjDigestFeeder,
)
from .macro.role import MacroGateRole
from .macro.state_store import MacroStateStore
from .pipeline import SlowLoopComponents, run_slow_loop
from .scout.context_builder import ScoutContextBuilder
from .scout.feeders.stub import (
    StubMarketSummaryFeeder,
    StubNewsScoreFeeder,
    StubSectorMomentumFeeder,
    StubUniverseFeeder,
)
from .scout.role import ScoutRole
from .scout.screening_stub import ScreeningToolAdapterStub
from .strategy.engine import StrategyEngine
from .strategy.policy import load_policy
from .strategy.publisher import PositionSheetPublisher
from .strategy.risk_throttle import NoOpRiskThrottle

OWNER = "slow_loop"

logger = logging.getLogger(__name__)


def _try_build_chat_model() -> Any | None:
    """vLLM EXAONE 을 LangChain ChatOpenAI 로 감싸 반환. 미구성 시 None."""
    url = os.environ.get("VLLM_LLM_URL")
    model = os.environ.get("VLLM_LLM_MODEL")
    if not (url and model):
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.warning("langchain_openai 미설치 — LLM 호출 비활성. extras [slow_loop] 필요")
        return None
    return ChatOpenAI(
        model=model,
        base_url=url,
        api_key="not-needed",
        temperature=0.1,
    )


def _build_slow_loop_components(redis_client: aioredis.Redis) -> SlowLoopComponents | None:
    """SlowLoopComponents 조립. chat_model 없으면 None."""
    chat_model = _try_build_chat_model()
    if chat_model is None:
        return None

    observer = NullObserver()
    orchestrator = Orchestrator(
        role_registry=RoleRegistry.of(ScoutRole(), MacroGateRole()),
        tool_registry=ToolRegistry(),
        model_router=SingleModelRouter(chat_model),
        memory=NullMemoryStore(),
        hitl=NullHITLChannel(),
        observer=observer,
        resilience=default_resilience(),
    )

    # TODO(phase-2.9-slice3): feeder 를 Track E 실제 구현으로 교체
    # (news_scores: news_pipeline_kor, market_snapshot: KIS/legacy_daily_prices 등)
    scout_builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsScoreFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro_builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )

    policy = load_policy()
    engine = StrategyEngine(policy=policy, risk_throttle=NoOpRiskThrottle())
    publisher = PositionSheetPublisher(client=redis_client)
    state_store = MacroStateStore(client=redis_client)

    return SlowLoopComponents(
        orchestrator=orchestrator,
        scout_builder=scout_builder,
        macro_builder=macro_builder,
        screening=ScreeningToolAdapterStub(),
        engine=engine,
        publisher=publisher,
        state_store=state_store,
        observer=observer,
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

        components = _build_slow_loop_components(redis_client)
        if components is None:
            logger.warning(
                "slow_loop components 미구성 (VLLM_LLM_URL/MODEL 또는 langchain_openai 누락). "
                "스케줄은 기동하지만 handler 는 skip"
            )

        async def scout_daily(trigger: str = "scheduled") -> None:
            if components is None:
                logger.warning("scout_daily skipped: components unavailable")
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
            result = await run_slow_loop(
                components,
                as_of_date=as_of_date,
                as_of_dt=now,
                macro_run_id=macro_run_id,
                scout_run_id=scout_run_id,
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

        engine = create_engine(cfg.postgres)
        stack.push_async_callback(engine.dispose)

        scheduler = SchedulerRunner(
            owner=OWNER,
            handlers=handlers,
            store=PostgresSchedulerStore(engine),
            redis_client=redis_client,
            timezone_name=cfg.timezone,
        )
        await scheduler.start()
        stack.push_async_callback(scheduler.stop)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        logger.info("slow_loop runner ready — waiting for signals")
        await stop_event.wait()
        logger.info("slow_loop runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
