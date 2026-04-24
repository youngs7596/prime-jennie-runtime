"""Job Worker long-running runner (entrypoint for `job-worker` 컨테이너).

v2 `prime_jennie/services/jobs/app.py` (FastAPI, 2883줄) 대체. FastAPI 엔드포인트
대신 apscheduler 기반 실행으로 전환한 이유:
- Airflow 종속을 제거하기로 결정 (slice2). job-worker 의 역할은 DAG http_conn 을
  받아서 함수를 돌려주는 단순 shell 이었다.
- v3 의 `scheduled_jobs` 테이블이 단일 진실 공급원. owner='job_worker' 의 row 가
  여기서 실행된다.

실행:
    uv run python -m prime_jennie_runtime.jobs.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.briefing.reporter import LLMCaller, build_chat_llm_caller
from prime_jennie_runtime.infra.config import AppConfig, TelegramConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner
from prime_jennie_runtime.news_pipeline_global.pipeline import (
    build_digest as global_news_build_digest,
)
from prime_jennie_runtime.news_pipeline_global.pipeline import (
    crawl_cycle as global_news_crawl_cycle,
)
from prime_jennie_runtime.news_pipeline_kor.adapters.naver_crawler import (
    NaverNewsCrawler,
)

from .analytics import analyst_feedback, analyze_ai_performance
from .asset_snapshot import daily_asset_snapshot
from .briefing_glue import daily_briefing_report
from .council_macro import (
    macro_collect_global,
    macro_collect_korea,
    macro_quick,
    macro_validate_store,
)
from .disclosures import collect_dart_filings
from .factor_analysis import weekly_factor_analysis
from .full_market_daily import collect_full_market_data
from .fundamentals import (
    collect_consensus,
    collect_naver_roe,
    collect_quarterly_financials,
)
from .investor_data import collect_foreign_holding, collect_investor_trading
from .maintenance import cleanup_old_data, contract_smoke_test, update_naver_sectors
from .market_data import (
    collect_index_daily_prices,
    collect_us_market,
    refresh_market_caps,
)
from .minute_chart import collect_minute_chart
from .positions import sync_positions
from .stock_masters import seed_stock_masters
from .wsj_gmail_ingest import wsj_gmail_ingest

OWNER = "job_worker"

logger = logging.getLogger(__name__)


def _build_briefing_llm_caller() -> LLMCaller | None:
    """daily_briefing_report / wsj_gmail_ingest 용 LLMCaller.

    DeepSeek chat (openai-compatible). DEEPSEEK_API_KEY 없으면 None → briefing 은
    fallback HTML, WSJ 요약은 skip.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.info("DEEPSEEK_API_KEY 없음 — briefing/WSJ 는 fallback/skip")
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.warning("langchain_openai 미설치 — briefing/WSJ 는 fallback/skip")
        return None
    model = ChatOpenAI(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        temperature=0.3,
    )
    return build_chat_llm_caller(model)


def build_handlers(
    *,
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    kis_gateway_url: str,
    engine: AsyncEngine,
    telegram_config: TelegramConfig | None,
) -> dict[str, Callable[..., Awaitable[Any]]]:
    """handler_key → async callable 매핑.

    새 job 포팅 시 여기에 등록. kwargs 는 scheduled_jobs.kwargs 에서 옴.
    """

    async def h_cleanup_old_data(days: int = 365) -> None:
        await cleanup_old_data(pool, days=days)

    async def h_macro_validate_store() -> None:
        await macro_validate_store(redis_client)

    bok_ecos_api_key = os.environ.get("BOK_ECOS_API_KEY") or None
    dart_api_key = os.environ.get("DART_API_KEY") or ""

    async def h_macro_collect_global() -> None:
        await macro_collect_global(redis_client, http, bok_ecos_api_key=bok_ecos_api_key)

    async def h_macro_collect_korea() -> None:
        await macro_collect_korea(redis_client, http, bok_ecos_api_key=bok_ecos_api_key)

    async def h_macro_quick() -> None:
        await macro_quick(redis_client, http, bok_ecos_api_key=bok_ecos_api_key)

    async def h_contract_smoke_test() -> None:
        # 뉴스 크롤러는 Track E 의 공통 HTTP 클라이언트를 재사용하지 않고
        # 각자 client 를 열고 닫는다 (v2 에서도 분리). 핸들러 수명과 일치.
        news_crawler = NaverNewsCrawler(max_pages=1)
        await contract_smoke_test(http, news_crawler)

    async def h_update_naver_sectors() -> None:
        await update_naver_sectors(pool, http)

    async def h_refresh_market_caps() -> None:
        await refresh_market_caps(pool, http, kis_gateway_url)

    async def h_collect_index_daily_prices(days: int = 250) -> None:
        await collect_index_daily_prices(pool, http, days=days)

    async def h_collect_us_market(days: int = 500) -> None:
        await collect_us_market(pool, http, days=days)

    async def h_collect_investor_trading() -> None:
        await collect_investor_trading(pool, http)

    async def h_collect_foreign_holding() -> None:
        await collect_foreign_holding(pool, http)

    async def h_collect_dart_filings(days: int = 7) -> None:
        await collect_dart_filings(pool, http, api_key=dart_api_key, days=days)

    async def h_collect_consensus() -> None:
        await collect_consensus(pool, http)

    async def h_collect_naver_roe() -> None:
        await collect_naver_roe(pool, http)

    async def h_collect_quarterly_financials() -> None:
        await collect_quarterly_financials(pool, http)

    async def h_seed_stock_masters(market: str = "KOSPI") -> None:
        await seed_stock_masters(pool, http, market=market)

    async def h_daily_asset_snapshot() -> None:
        # 전용 httpx.AsyncClient 는 함수 내부에서 생성 — stale keepalive 격리.
        await daily_asset_snapshot(pool, kis_gateway_url)

    async def h_sync_positions(dry_run: bool = True, stop_loss_pct: float = 6.0) -> None:
        await sync_positions(
            pool,
            http,
            redis_client,
            kis_gateway_url,
            dry_run=dry_run,
            stop_loss_pct=stop_loss_pct,
        )

    async def h_analyze_ai_performance(period_days: int = 30) -> None:
        await analyze_ai_performance(pool, redis_client, period_days=period_days)

    async def h_analyst_feedback() -> None:
        await analyst_feedback(redis_client)

    async def h_weekly_factor_analysis(period_days: int = 30) -> None:
        await weekly_factor_analysis(pool, redis_client, period_days=period_days)

    async def h_collect_minute_chart(top_n: int = 30) -> None:
        await collect_minute_chart(pool, http, kis_gateway_url, top_n=top_n)

    async def h_collect_full_market_data(top_n: int = 300, days: int = 30) -> None:
        await collect_full_market_data(pool, http, kis_gateway_url, top_n=top_n, days=days)

    briefing_llm_caller = _build_briefing_llm_caller()

    async def h_daily_briefing_report() -> None:
        await daily_briefing_report(
            engine,
            http,
            telegram_config=telegram_config,
            llm_caller=briefing_llm_caller,
            redis_client=redis_client,
        )

    async def h_global_news_crawl() -> None:
        await global_news_crawl_cycle(pool, http)

    async def h_global_news_digest(lookback_hours: int = 24) -> None:
        await global_news_build_digest(pool, http, lookback_hours=lookback_hours)

    wsj_creds_path = os.environ.get(
        "WSJ_GMAIL_CREDENTIALS_PATH", "/app/infra/secrets/gmail/credentials.json"
    )
    wsj_token_path = os.environ.get("WSJ_GMAIL_TOKEN_PATH", "/app/infra/secrets/gmail/token.json")

    async def h_wsj_gmail_ingest() -> None:
        await wsj_gmail_ingest(
            pool,
            http,
            redis_client,
            credentials_path=wsj_creds_path,
            token_path=wsj_token_path,
            telegram_config=telegram_config,
            llm_caller=briefing_llm_caller,
        )

    return {
        "cleanup_old_data": h_cleanup_old_data,
        "macro_validate_store": h_macro_validate_store,
        "macro_collect_global": h_macro_collect_global,
        "macro_collect_korea": h_macro_collect_korea,
        "macro_quick": h_macro_quick,
        "contract_smoke_test": h_contract_smoke_test,
        "update_naver_sectors": h_update_naver_sectors,
        "refresh_market_caps": h_refresh_market_caps,
        "collect_index_daily_prices": h_collect_index_daily_prices,
        "collect_us_market": h_collect_us_market,
        "collect_investor_trading": h_collect_investor_trading,
        "collect_foreign_holding": h_collect_foreign_holding,
        "collect_dart_filings": h_collect_dart_filings,
        "collect_consensus": h_collect_consensus,
        "collect_naver_roe": h_collect_naver_roe,
        "collect_quarterly_financials": h_collect_quarterly_financials,
        "seed_stock_masters": h_seed_stock_masters,
        "daily_asset_snapshot": h_daily_asset_snapshot,
        "sync_positions": h_sync_positions,
        "analyze_ai_performance": h_analyze_ai_performance,
        "analyst_feedback": h_analyst_feedback,
        "weekly_factor_analysis": h_weekly_factor_analysis,
        "collect_minute_chart": h_collect_minute_chart,
        "collect_full_market_data": h_collect_full_market_data,
        "daily_briefing_report": h_daily_briefing_report,
        "global_news_crawl": h_global_news_crawl,
        "global_news_digest": h_global_news_digest,
        "wsj_gmail_ingest": h_wsj_gmail_ingest,
    }


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient())

        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="job-worker")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

        pool = await asyncpg.create_pool(
            host=cfg.postgres.host,
            port=cfg.postgres.port,
            user=cfg.postgres.user,
            password=cfg.postgres.password,
            database=cfg.postgres.db,
            min_size=1,
            max_size=4,
        )
        stack.push_async_callback(pool.close)

        kis_gateway_url = os.environ.get("KIS_GATEWAY_URL", cfg.kis.gateway_url).rstrip("/")

        engine = create_engine(cfg.postgres)
        stack.push_async_callback(engine.dispose)

        telegram_cfg: TelegramConfig | None = (
            cfg.telegram
            if (cfg.telegram.bot_token and cfg.telegram.chat_id) or cfg.telegram.dry_run
            else None
        )

        handlers = build_handlers(
            pool=pool,
            http=http,
            redis_client=redis_client,
            kis_gateway_url=kis_gateway_url,
            engine=engine,
            telegram_config=telegram_cfg,
        )

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

        logger.info("job_worker runner ready — waiting for signals")
        await stop_event.wait()
        logger.info("job_worker runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
