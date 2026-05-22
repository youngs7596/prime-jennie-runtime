"""Dashboard 테스트 픽스처 — in-memory SQLite (async) + fakeredis.

Postgres 특화 JSONB/NOW() 는 대부분 `text()` SQL 이므로 SQLite 에서도 동작하도록
스키마를 간소화해서 생성한다. JSONB → TEXT, TIMESTAMPTZ → TIMESTAMP 로 매핑.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from prime_jennie_runtime.dashboard.app import create_app
from prime_jennie_runtime.dashboard.deps import get_redis, get_session_factory

_SCHEMA_STATEMENTS = [
    # position_sheets
    """
    CREATE TABLE IF NOT EXISTS position_sheets (
        sheet_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL DEFAULT '1.1',
        generated_at TIMESTAMP NOT NULL,
        valid_until TIMESTAMP NOT NULL,
        ticker TEXT NOT NULL,
        strategy_tag TEXT NOT NULL,
        sheet_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # executions
    """
    CREATE TABLE IF NOT EXISTS executions (
        execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet_id TEXT NOT NULL,
        side TEXT NOT NULL,
        price NUMERIC NOT NULL,
        qty INT NOT NULL,
        executed_at TIMESTAMP NOT NULL,
        slippage_bps NUMERIC,
        metadata_json TEXT DEFAULT '{}'
    )
    """,
    # outcomes
    """
    CREATE TABLE IF NOT EXISTS outcomes (
        sheet_id TEXT PRIMARY KEY,
        entry_price NUMERIC NOT NULL,
        exit_price NUMERIC,
        holding_period_s INT,
        pnl_krw NUMERIC,
        pnl_pct NUMERIC,
        exit_reason TEXT,
        closed_at TIMESTAMP,
        metadata_json TEXT DEFAULT '{}'
    )
    """,
    # macro_runs
    """
    CREATE TABLE IF NOT EXISTS macro_runs (
        macro_run_id TEXT PRIMARY KEY,
        generated_at TIMESTAMP NOT NULL,
        trigger_reason TEXT NOT NULL,
        gate TEXT NOT NULL,
        size_multiplier NUMERIC NOT NULL,
        reasoning TEXT,
        top_risks_json TEXT,
        confidence TEXT,
        news_digest_ref TEXT,
        next_review_hint TEXT,
        prompt_version TEXT,
        model_used TEXT,
        cost_usd NUMERIC,
        latency_ms INT,
        auto_override BOOLEAN DEFAULT 0,
        metadata_json TEXT DEFAULT '{}'
    )
    """,
    # scout_runs (+ migration 012 context_snapshot_json). model_used / cost_usd 는
    # 스키마에 남아 있으나 결정론 scout (2026-05-22 Phase 1) 는 쓰지 않아 항상 NULL.
    """
    CREATE TABLE IF NOT EXISTS scout_runs (
        scout_run_id TEXT PRIMARY KEY,
        generated_at TIMESTAMP NOT NULL,
        code_hash TEXT NOT NULL,
        code_text TEXT NOT NULL,
        hypothesis TEXT,
        candidates_count INT,
        model_used TEXT,
        prompt_version TEXT,
        cost_usd NUMERIC,
        metadata_json TEXT DEFAULT '{}',
        context_snapshot_json TEXT DEFAULT '{}'
    )
    """,
    # screening_candidates (migration 012) — raw 후보 전수
    """
    CREATE TABLE IF NOT EXISTS screening_candidates (
        scout_run_id TEXT NOT NULL,
        rank INT NOT NULL,
        ticker TEXT NOT NULL,
        strategy_tag TEXT NOT NULL,
        conviction NUMERIC,
        entry_hint_json TEXT,
        exit_hint_json TEXT,
        factors_json TEXT,
        notes TEXT,
        promoted_to_sheet_id TEXT,
        rejection_reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scout_run_id, rank)
    )
    """,
    # stock_masters — /scout/candidates 가 ticker → 종목명 LEFT JOIN
    """
    CREATE TABLE IF NOT EXISTS stock_masters (
        stock_code TEXT PRIMARY KEY,
        stock_name TEXT NOT NULL,
        market TEXT,
        market_cap NUMERIC,
        sector_naver TEXT,
        sector_group TEXT,
        is_active BOOLEAN DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # news_articles (migrations/004)
    """
    CREATE TABLE IF NOT EXISTS news_articles (
        article_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        title TEXT,
        body TEXT,
        published_at TIMESTAMP,
        source_url TEXT,
        source_name TEXT
    )
    """,
    # news_events (migrations/014) — SQLite 용으로 단순화 (TEXT[] → TEXT JSON string)
    """
    CREATE TABLE IF NOT EXISTS news_events (
        article_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        published_at TIMESTAMP,
        event_type TEXT NOT NULL,
        impact_level TEXT NOT NULL,
        sentiment TEXT NOT NULL,
        sentiment_score NUMERIC,
        time_horizon TEXT,
        keywords TEXT,
        sector_tags TEXT,
        financial_signals TEXT,
        confidence NUMERIC,
        model TEXT,
        analyzed_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # scheduled_jobs
    """
    CREATE TABLE IF NOT EXISTS scheduled_jobs (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        handler_key TEXT NOT NULL,
        cron TEXT NOT NULL,
        kwargs TEXT NOT NULL DEFAULT '{}',
        enabled BOOLEAN NOT NULL DEFAULT 1,
        last_run_at TIMESTAMP,
        last_status TEXT,
        last_error TEXT,
        last_duration_ms INTEGER,
        next_run_at TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # scheduled_job_runs
    """
    CREATE TABLE IF NOT EXISTS scheduled_job_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        status TEXT NOT NULL,
        error TEXT,
        duration_ms INTEGER
    )
    """,
    # daily_asset_snapshots (migration 009)
    """
    CREATE TABLE IF NOT EXISTS daily_asset_snapshots (
        snapshot_date DATE PRIMARY KEY,
        total_asset BIGINT NOT NULL,
        cash_balance BIGINT NOT NULL,
        stock_eval_amount BIGINT NOT NULL,
        position_count INTEGER NOT NULL,
        total_profit_loss BIGINT,
        realized_profit_loss BIGINT
    )
    """,
]


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        for stmt in _SCHEMA_STATEMENTS:
            from sqlalchemy import text as _t

            await conn.execute(_t(stmt))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def redis_client():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def app(monkeypatch, session_factory, redis_client):
    """Lifespan 을 거치지 않고 직접 dep override."""
    # Patch create_engine used in create_app lifespan so no real DB is touched.
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_redis] = lambda: redis_client
    return app
