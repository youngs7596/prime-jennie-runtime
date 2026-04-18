"""Slow Loop long-running runner (entrypoint for `slow-loop` 컨테이너).

`scheduled_jobs` (owner='slow_loop') 에 등록된 cron 을 apscheduler 가 트리거하면
`scout_daily` handler 가 `run_slow_loop()` 를 호출한다.

Role tier 매핑 (2026-04-19 개정 — Scout 도 코드 생성 품질 우선):
  - Scout (strong)    → Claude Opus 4.7 (ANTHROPIC_API_KEY)
  - Macro (reasoning) → Claude Opus 4.7 (ANTHROPIC_API_KEY)
  - Scout shadow      → DeepSeek chat   (DEEPSEEK_API_KEY, 비교 평가 데이터 축적)
  - Macro shadow      → DeepSeek (chat 기본, reasoner override 가능)

Scout 는 하루 수회 cron 에 한 번씩 Python 코드를 생성만 하므로 per-ticker 비용이
없고, 코드 품질이 screening 후보 분포 전체를 결정한다. 저렴한 모델보다 Opus
품질이 더 중요해서 primary 를 Opus 로 바꾸고 DeepSeek 는 shadow 로 병렬 축적.

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
    NullObserver,
    Orchestrator,
    RoleRegistry,
    TieredModelRouter,
    ToolRegistry,
    default_resilience,
)

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner
from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.screening_executor.adapter import ScreeningToolAdapter

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
from .strategy.engine import StrategyEngine
from .strategy.policy import load_policy
from .strategy.publisher import PositionSheetPublisher
from .strategy.risk_throttle import NoOpRiskThrottle

OWNER = "slow_loop"

logger = logging.getLogger(__name__)


def _try_build_tiered_router() -> Any | None:
    """Role tier → ChatModel 매핑 반환. 필수 키 누락 시 None.

    tier 매핑 (2026-04-19 개정):
      - strong    → Scout      = Claude Opus 4.7 (ANTHROPIC_API_KEY)
      - reasoning → Macro Gate = Claude Opus 4.7 (ANTHROPIC_API_KEY)
      - shadow_strong    → Scout shadow = DeepSeek chat (DEEPSEEK_API_KEY)
      - shadow_reasoning → Macro shadow = DeepSeek (chat/reasoner)
      - fast      → 현재 slow_loop 에선 사용 X (news_pipeline 이 별도로 vLLM EXAONE 호출)

    Opus 하나는 Scout + Macro 둘 다 같은 인스턴스로 재사용 (stateless API 라 무해).
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
    # Shadow = Opus 와 reasoning 동급 비교. DeepSeek 계보 업데이트:
    #   - deepseek-chat: V3.2 (최신 플래그십, hybrid thinking/non-thinking) ← Opus 대응
    #   - deepseek-reasoner: R1 (구세대 reasoning 전용, 스키마 literal 위반 관찰됨)
    # Scout 용 strong tier 와 같은 identifier 지만 "chat tier" 가 아니라 실상 V3.2 flagship.
    deepseek_shadow_model = os.environ.get("DEEPSEEK_SHADOW_MODEL", "deepseek-chat")

    # DeepSeek chat: function_calling 만 지원 (json_schema response_format 미지원).
    class _DeepSeekChatOpenAI(ChatOpenAI):
        def with_structured_output(self, schema, *, method=None, **kwargs):  # type: ignore[override]
            return super().with_structured_output(
                schema, method=method or "function_calling", **kwargs
            )

    # DeepSeek reasoner (R1): tool_choice/function_calling 미지원 → json_mode 로.
    # 프롬프트에 JSON 스키마가 이미 명시되어 있어야 함 (Macro 프롬프트는 그렇게 구성됨).
    class _DeepSeekReasonerOpenAI(ChatOpenAI):
        def with_structured_output(self, schema, *, method=None, **kwargs):  # type: ignore[override]
            return super().with_structured_output(schema, method=method or "json_mode", **kwargs)

    # 주: claude-opus-4-7 은 temperature 파라미터 미지원 (API 400).
    opus = ChatAnthropic(
        model=anthropic_model,
        api_key=anthropic_key,
    )
    # Primary Scout + Macro 둘 다 Opus (stateless API 라 인스턴스 재사용 무해).
    strong = opus
    reasoning = opus

    # Scout shadow = DeepSeek chat (저비용 거울, 비교 평가용 데이터 축적)
    scout_shadow = _DeepSeekChatOpenAI(
        model=deepseek_model,
        api_key=deepseek_key,
        base_url=deepseek_base,
        temperature=0.1,
    )

    # Macro shadow = 기본 deepseek-chat (V3.2 flagship, hybrid). reasoner 로 override 하면
    # json_mode 로 자동 전환되도록 모델 ID 로 wrapper 선택.
    shadow_cls = (
        _DeepSeekReasonerOpenAI if "reasoner" in deepseek_shadow_model else _DeepSeekChatOpenAI
    )
    shadow_reasoning = shadow_cls(
        model=deepseek_shadow_model,
        api_key=deepseek_key,
        base_url=deepseek_base,
        temperature=0.1,
    )
    return {
        "strong": strong,
        "reasoning": reasoning,
        "default": strong,
        # shadow tier — primary orchestrator 와 동일 role 이 다른 모델로 병렬 평가
        "shadow_strong": scout_shadow,
        "shadow_reasoning": shadow_reasoning,
    }


def _build_slow_loop_components(
    redis_client: aioredis.Redis,
    db_engine: Any = None,
) -> SlowLoopComponents | None:
    """SlowLoopComponents 조립. tier 라우터 구성 실패 시 None.

    db_engine 이 주어지면 macro_runs/scout_runs 를 persist. None 이면 기록 없이 실행.
    """
    tiers = _try_build_tiered_router()
    if tiers is None:
        return None

    observer = NullObserver()
    orchestrator = Orchestrator(
        role_registry=RoleRegistry.of(ScoutRole(), MacroGateRole()),
        tool_registry=ToolRegistry(),
        model_router=TieredModelRouter(tiers),
        memory=NullMemoryStore(),
        hitl=NullHITLChannel(),
        observer=observer,
        resilience=default_resilience(),
    )

    # Shadow orchestrator — Macro + Scout 을 DeepSeek 로 병렬 평가. Opus 결정이 primary.
    # MACRO_SHADOW_ENABLED=0 / SCOUT_SHADOW_ENABLED=0 으로 개별 비활성화 가능.
    # shadow_orchestrator 자체는 shadow_* 태그 중 하나라도 활성이면 구성됨.
    shadow_orchestrator: Orchestrator | None = None
    macro_shadow_on = os.environ.get("MACRO_SHADOW_ENABLED", "1") == "1"
    scout_shadow_on = os.environ.get("SCOUT_SHADOW_ENABLED", "1") == "1"
    if (macro_shadow_on or scout_shadow_on) and (
        "shadow_reasoning" in tiers or "shadow_strong" in tiers
    ):
        shadow_tiers = {
            # shadow 가 꺼진 role 은 primary 모델 그대로 재사용 (shadow 호출 자체를
            # pipeline 단에서 skip 하므로 실사용 안 됨 — fallback 안전치)
            "strong": tiers["shadow_strong"] if scout_shadow_on else tiers["strong"],
            "reasoning": tiers["shadow_reasoning"] if macro_shadow_on else tiers["reasoning"],
            "default": tiers["shadow_strong"] if scout_shadow_on else tiers["default"],
        }
        shadow_orchestrator = Orchestrator(
            role_registry=RoleRegistry.of(ScoutRole(), MacroGateRole()),
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
            RealNewsScoreFeeder,
            RealSectorMomentumFeeder,
            RealUniverseFeeder,
        )

        scout_builder = ScoutContextBuilder(
            universe=RealUniverseFeeder(engine=db_engine),
            news=RealNewsScoreFeeder(engine=db_engine),
            sector=RealSectorMomentumFeeder(engine=db_engine),
            market=RealMarketSummaryFeeder(engine=db_engine),
        )
    else:
        scout_builder = ScoutContextBuilder(
            universe=StubUniverseFeeder(),
            news=StubNewsScoreFeeder(),
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
    engine = StrategyEngine(policy=policy, risk_throttle=NoOpRiskThrottle())
    publisher = PositionSheetPublisher(client=redis_client, db_engine=db_engine)
    state_store = MacroStateStore(client=redis_client)

    return SlowLoopComponents(
        orchestrator=orchestrator,
        scout_builder=scout_builder,
        macro_builder=macro_builder,
        screening=ScreeningToolAdapter(
            backend=os.environ.get("SCREENING_BACKEND", "subprocess"),
            timeout_s=float(os.environ.get("SCREENING_TIMEOUT_S", "300")),
        ),
        engine=engine,
        publisher=publisher,
        state_store=state_store,
        observer=observer,
        db_engine=db_engine,
        shadow_orchestrator=shadow_orchestrator,
        redis_client=redis_client,
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

        components = _build_slow_loop_components(redis_client, db_engine=engine)
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
