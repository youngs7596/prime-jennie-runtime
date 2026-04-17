"""실 Postgres 대상 apply/rollback 통합 테스트.

`POSTGRES_TEST_DSN` 환경변수가 있을 때만 실행. 없으면 skip.

테스트 DSN 예:
    POSTGRES_TEST_DSN="postgresql://pj_test:pj_test@localhost:5432/pj_migration_test"

테스트는 해당 DB 의 public schema 를 싹 비우고 001..010 을 순서대로 apply, 테이블 존재
확인, 다시 public schema 를 drop/recreate 하여 rollback 시뮬레이션. 따라서 **전용 테스트
DB 를 쓸 것** — 운영 DB 로 돌리면 데이터 손실.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    import asyncpg  # type: ignore[import-not-found]

    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False


DSN = os.getenv("POSTGRES_TEST_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

ALL_MIGRATION_FILES = sorted(MIGRATIONS_DIR.glob("*.sql"))

EXPECTED_TABLES_AFTER_APPLY = {
    # 001
    "position_sheets",
    "executions",
    "outcomes",
    "scout_runs",
    "macro_runs",
    "meta_prs",
    # 002
    "legacy_signal_logs",
    "legacy_trade_logs",
    "legacy_quant_scores",
    # 003
    "daily_prices",
    "minute_prices",
    # 004
    "news_sentiments",
    "news_articles",
    # 005
    "scheduled_jobs",
    "scheduled_job_runs",
    # 006
    "stock_masters",
    "configs",
    # 007
    "stock_investor_tradings",
    "stock_fundamentals",
    "stock_disclosures",
    "stock_consensus",
    "index_daily_prices",
    "us_market_daily",
    # 008
    "daily_macro_insights",
    "global_macro_snapshots",
    # 009
    "positions",
    "daily_asset_snapshots",
    "watchlist_histories",
    "daily_quant_scores",
    # 010
    "legacy_news_sentiments",
}


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HAS_ASYNCPG, reason="asyncpg not installed"),
    pytest.mark.skipif(DSN is None, reason="POSTGRES_TEST_DSN not set"),
]


async def _reset_public_schema(conn) -> None:
    """public schema drop + recreate. 마이그레이션이 쓰는 role 까지 같이 drop.

    001 의 DO $$ ... pg_roles 체크 블록은 역할이 있으면 재생성 안 함. 역할을 drop
    하려면 소유 객체가 모두 정리되어 있어야 해서, schema drop 후 role drop 순서.
    """
    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE;")
    await conn.execute("CREATE SCHEMA public;")
    await conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER;")
    await conn.execute("GRANT ALL ON SCHEMA public TO PUBLIC;")
    for role in ("pj_runtime", "pj_meta", "pj_ui"):
        try:
            await conn.execute(
                f"REASSIGN OWNED BY {role} TO CURRENT_USER; DROP OWNED BY {role}; DROP ROLE {role};"
            )
        except asyncpg.exceptions.UndefinedObjectError:
            pass


@pytest.mark.asyncio
async def test_apply_all_migrations_then_rollback():
    conn = await asyncpg.connect(dsn=DSN)
    try:
        await _reset_public_schema(conn)

        # apply
        for path in ALL_MIGRATION_FILES:
            sql = path.read_text()
            await conn.execute(sql)

        # verify
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        actual = {r["tablename"] for r in rows}
        missing = EXPECTED_TABLES_AFTER_APPLY - actual
        assert not missing, f"missing tables after full apply: {missing}"

        # 006 포팅 — FK 가 실제로 걸렸는지 스팟 체크.
        fk_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.table_constraints
             WHERE constraint_type='FOREIGN KEY'
               AND table_name='stock_investor_tradings'
            """
        )
        assert fk_count >= 1, "stock_investor_tradings FK to stock_masters not applied"

        # idempotent 재실행 검증 — 다시 apply 해도 에러 없어야 함.
        for path in ALL_MIGRATION_FILES:
            await conn.execute(path.read_text())

        # rollback: schema drop 이 곧 rollback.
        await _reset_public_schema(conn)
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert not [r for r in rows if r["tablename"] in EXPECTED_TABLES_AFTER_APPLY]
    finally:
        await conn.close()
