"""v3 `scheduled_jobs` 초기 시드.

우선 news_pipeline.crawl_cycle 만 시드. 추가 owner 는 다음 슬라이스 (price_scheduler,
slow_loop, fast_loop) 에서 확장.

- 기본 동작 (idempotent): 해당 id 가 없을 때만 INSERT. 수동으로 cron/universe 를
  바꿔둔 경우 덮어쓰지 않는다.
- `--force`: 기존 row 의 cron/kwargs/handler_key 도 강제로 덮어씀 (운영 중 사용 주의).

실행:
    uv run python -m scripts.seed_scheduled_jobs [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import asyncpg

logger = logging.getLogger("seed_scheduled_jobs")


@dataclass(frozen=True)
class SeedJob:
    id: str
    owner: str
    handler_key: str
    cron: str
    kwargs: dict
    enabled: bool = True


# 초기 시드 — news_pipeline 만.
# universe 는 v2 에서 자주 모니터하던 대형주 샘플. 영석님이 control-ui 또는 SQL 로
# 직접 조정. 스케줄은 v2 news-pipeline AGENTS.md 의 "10분 주기" 원칙 + 장중 한정.
_DEFAULT_UNIVERSE = ["005930", "000660", "035720", "005380", "051910"]

# 초기 시드 — Phase 2.9. universe 는 v2 운영 시 자주 모니터하던 대형주 샘플.
# 영석님이 control-ui 또는 SQL 로 조정. cron 은 v2 DAG 의 schedule 을 최대한 맞춤.
SEEDS: list[SeedJob] = [
    # Track E — 뉴스 감성 (v2 news-pipeline 은 10분 주기 내부 루프, 장중 한정)
    SeedJob(
        id="news_pipeline.crawl_cycle",
        owner="news_pipeline",
        handler_key="crawl_cycle",
        cron="*/10 9-15 * * 1-5",
        kwargs={"universe": _DEFAULT_UNIVERSE},
    ),
    # Track C — KIS 분봉/일봉 (v2 collect_minute_chart: */5 9-15 * * 1-5)
    SeedJob(
        id="price_scheduler.collect_minute",
        owner="price_scheduler",
        handler_key="collect_minute",
        cron="*/5 9-15 * * 1-5",
        kwargs={"universe": _DEFAULT_UNIVERSE},
    ),
    # v2 daily_market_data_collector: 0 16 * * 1-5
    SeedJob(
        id="price_scheduler.collect_daily",
        owner="price_scheduler",
        handler_key="collect_daily",
        cron="0 16 * * 1-5",
        kwargs={"universe": _DEFAULT_UNIVERSE, "days": 150},
    ),
    # Track B — scout/macro 주기 (v2 scout_job_dag: 30 8-14 * * 1-5)
    SeedJob(
        id="slow_loop.scout_daily",
        owner="slow_loop",
        handler_key="scout_daily",
        cron="30 8-14 * * 1-5",
        kwargs={"trigger": "scout_daily"},
    ),
    # Track B — job-worker (v2 utility_jobs_dag cleanup: 0 3 * * *)
    SeedJob(
        id="job_worker.cleanup_old_data",
        owner="job_worker",
        handler_key="cleanup_old_data",
        cron="0 3 * * *",
        kwargs={"days": 365},
    ),
    # Track B — macro_validate_store (v2 macro_dag validate: 30 8 * * 1-5)
    SeedJob(
        id="job_worker.macro_validate_store",
        owner="job_worker",
        handler_key="macro_validate_store",
        cron="30 8 * * 1-5",
        kwargs={},
    ),
    # Track B — contract_smoke_test (v2 utility_jobs_dag: 0 21 * * *)
    SeedJob(
        id="job_worker.contract_smoke_test",
        owner="job_worker",
        handler_key="contract_smoke_test",
        cron="0 21 * * *",
        kwargs={},
    ),
    # Track B — macro collect global/korea (v2 macro_dag: 40 7,11 * * 1-5).
    # v2 는 global + korea 를 병렬로 돌리지만 korea 는 global 위임이라 중복 수집이
    # 된다. v3 도 동일 스케줄을 유지해서 validate_store 의 fallback 을 깨지 않는다.
    SeedJob(
        id="job_worker.macro_collect_global",
        owner="job_worker",
        handler_key="macro_collect_global",
        cron="40 7,11 * * 1-5",
        kwargs={},
    ),
    SeedJob(
        id="job_worker.macro_collect_korea",
        owner="job_worker",
        handler_key="macro_collect_korea",
        cron="40 7,11 * * 1-5",
        kwargs={},
    ),
    # Track B — macro_quick (v2 enhanced_macro_quick DAG: */5 9-15 * * 1-5).
    # v2 는 snapshot 수집 + intraday risk throttle 이지만 v3 는 아직 Context 모델이
    # 없어 snapshot 만 갱신한다. throttle 레이어는 Context 포팅 이후 슬라이스.
    SeedJob(
        id="job_worker.macro_quick",
        owner="job_worker",
        handler_key="macro_quick",
        cron="*/5 9-15 * * 1-5",
        kwargs={},
    ),
    # Track B — update_naver_sectors (v2 utility_jobs_dag: 0 20 * * 0, 주간).
    SeedJob(
        id="job_worker.update_naver_sectors",
        owner="job_worker",
        handler_key="update_naver_sectors",
        cron="0 20 * * 0",
        kwargs={},
    ),
    # Track B — refresh_market_caps (v2 utility_jobs_dag: 50 15 * * 1-5, 장마감 후).
    SeedJob(
        id="job_worker.refresh_market_caps",
        owner="job_worker",
        handler_key="refresh_market_caps",
        cron="50 15 * * 1-5",
        kwargs={},
    ),
    # Track B — collect_index_daily_prices (v2 utility_jobs_dag: 5 16 * * 1-5).
    SeedJob(
        id="job_worker.collect_index_daily_prices",
        owner="job_worker",
        handler_key="collect_index_daily_prices",
        cron="5 16 * * 1-5",
        kwargs={"days": 250},
    ),
    # Track B — collect_us_market (v2 utility_jobs_dag: 0 7 * * 2-6, 미장 마감 후 KST 아침).
    SeedJob(
        id="job_worker.collect_us_market",
        owner="job_worker",
        handler_key="collect_us_market",
        cron="0 7 * * 2-6",
        kwargs={"days": 500},
    ),
    # Track B — collect_investor_trading (v2 utility_jobs_dag: 30 18 * * 1-5, 장후 수급).
    SeedJob(
        id="job_worker.collect_investor_trading",
        owner="job_worker",
        handler_key="collect_investor_trading",
        cron="30 18 * * 1-5",
        kwargs={},
    ),
    # Track B — collect_foreign_holding (v2 utility_jobs_dag: 0 19 * * 1-5, 외국인 지분율).
    SeedJob(
        id="job_worker.collect_foreign_holding",
        owner="job_worker",
        handler_key="collect_foreign_holding",
        cron="0 19 * * 1-5",
        kwargs={},
    ),
    # Track B — collect_dart_filings (v2 utility_jobs_dag: 45 18 * * 1-5, DART 정기공시).
    SeedJob(
        id="job_worker.collect_dart_filings",
        owner="job_worker",
        handler_key="collect_dart_filings",
        cron="45 18 * * 1-5",
        kwargs={"days": 7},
    ),
    # Track B — collect_consensus (v2 utility_jobs_dag: 0 6 * * 1,4, 주간 월/목).
    SeedJob(
        id="job_worker.collect_consensus",
        owner="job_worker",
        handler_key="collect_consensus",
        cron="0 6 * * 1,4",
        kwargs={},
    ),
    # Track B — collect_naver_roe (v2 utility_jobs_dag: 0 3 1 * *, 월간 1일 03:00).
    SeedJob(
        id="job_worker.collect_naver_roe",
        owner="job_worker",
        handler_key="collect_naver_roe",
        cron="0 3 1 * *",
        kwargs={},
    ),
    # Track B — collect_quarterly_financials (v2 utility_jobs_dag: 0 4 15 1,4,7,10 *, 분기).
    SeedJob(
        id="job_worker.collect_quarterly_financials",
        owner="job_worker",
        handler_key="collect_quarterly_financials",
        cron="0 4 15 1,4,7,10 *",
        kwargs={},
    ),
    # Track B — daily_asset_snapshot (v2 utility_jobs_dag: 45 15 * * 1-5, 장마감 직후).
    SeedJob(
        id="job_worker.daily_asset_snapshot",
        owner="job_worker",
        handler_key="daily_asset_snapshot",
        cron="45 15 * * 1-5",
        kwargs={},
    ),
    # Track B — analyze_ai_performance (v2 utility_jobs_dag: 0 7 * * 1-5, 개장 전 분석).
    SeedJob(
        id="job_worker.analyze_ai_performance",
        owner="job_worker",
        handler_key="analyze_ai_performance",
        cron="0 7 * * 1-5",
        kwargs={"period_days": 30},
    ),
    # Track B — analyst_feedback (v2 utility_jobs_dag: 0 18 * * 1-5, analyze 결과 기반).
    SeedJob(
        id="job_worker.analyst_feedback",
        owner="job_worker",
        handler_key="analyst_feedback",
        cron="0 18 * * 1-5",
        kwargs={},
    ),
]


async def seed(force: bool, dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for job in SEEDS:
            if force:
                await conn.execute(
                    "INSERT INTO scheduled_jobs (id, owner, handler_key, cron, kwargs, enabled) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, $6) "
                    "ON CONFLICT (id) DO UPDATE SET owner=EXCLUDED.owner, "
                    "handler_key=EXCLUDED.handler_key, cron=EXCLUDED.cron, "
                    "kwargs=EXCLUDED.kwargs, enabled=EXCLUDED.enabled, updated_at=NOW()",
                    job.id,
                    job.owner,
                    job.handler_key,
                    job.cron,
                    json.dumps(job.kwargs),
                    job.enabled,
                )
                logger.info("upsert (force): %s", job.id)
            else:
                result = await conn.execute(
                    "INSERT INTO scheduled_jobs (id, owner, handler_key, cron, kwargs, enabled) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, $6) "
                    "ON CONFLICT (id) DO NOTHING",
                    job.id,
                    job.owner,
                    job.handler_key,
                    job.cron,
                    json.dumps(job.kwargs),
                    job.enabled,
                )
                inserted = result.endswith(" 1")
                logger.info("%s: %s", "insert" if inserted else "exists", job.id)
    finally:
        await conn.close()


def _dsn_from_env() -> str:
    # pj_runtime 로도 INSERT 가능하지만 seed 는 pj_admin 계정으로 돌리는 게 안전.
    user = os.environ.get("POSTGRES_ADMIN_USER", os.environ.get("POSTGRES_USER", "pj_admin"))
    password = os.environ.get(
        "POSTGRES_ADMIN_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "dev_admin")
    )
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "prime_jennie_v3")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="기존 row 의 cron/kwargs 도 덮어씀 (수동 편집 내용이 사라짐)",
    )
    args = parser.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    asyncio.run(seed(force=args.force, dsn=_dsn_from_env()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
