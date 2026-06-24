"""v3 `scheduled_jobs` 초기 시드.

추가 owner 는 슬라이스별로 확장. `news_pipeline` owner 는 2026-04-21 이후 scheduler
기반에서 Redis Stream 상시 소비 모델로 전환 → 이 seed 에 포함하지 않는다 (참조:
migration 013).

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


# universe 는 v2 에서 자주 모니터하던 대형주 샘플. 영석님이 control-ui 또는 SQL 로
# 직접 조정. (news_pipeline 은 stock_masters 전체 active 종목을 자체 로드하므로
# 여기 universe 에 의존하지 않음)
_DEFAULT_UNIVERSE = ["005930", "000660", "035720", "005380", "051910"]

# 초기 시드 — Phase 2.9. universe 는 v2 운영 시 자주 모니터하던 대형주 샘플.
# 영석님이 control-ui 또는 SQL 로 조정. cron 은 v2 DAG 의 schedule 을 최대한 맞춤.
SEEDS: list[SeedJob] = [
    # Track C — KIS 일봉 (v2 daily_market_data_collector: 0 16 * * 1-5).
    # 분봉은 `job_worker.collect_minute_chart` (top30+watchlist) 가 단일 수집자.
    # price_scheduler.collect_minute (5종목 sample universe) 은 Phase 2.9 시드 시점의
    # placeholder 였으나 collect_minute_chart 와 완전 중복(top30 안에 5종목 모두 포함)으로
    # 2026-05-08 obsolete 처리. 운영 DB 에서도 enabled=false. 새 환경에서도 시드하지
    # 않기 위해 SeedJob 자체 제거.
    #
    # collect_daily 도 2026-05-22 disabled — job_worker.collect_full_market_data
    # (top300 자동발견, _DEFAULT_UNIVERSE 5종목을 모두 포함) 가 운영 일봉 수집자로
    # 전환되며 완전 중복. collect_minute → collect_minute_chart 와 동일 패턴.
    # SeedJob 은 이력 보존을 위해 enabled=False 로 남긴다.
    SeedJob(
        id="price_scheduler.collect_daily",
        owner="price_scheduler",
        handler_key="collect_daily",
        cron="0 16 * * 1-5",
        kwargs={"universe": _DEFAULT_UNIVERSE, "days": 150},
        enabled=False,
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
    # 매크로 게이트 입력 (2026-06-24) — VKOSPI 일별 (CNBC). KRX 장마감 후 반영되므로
    # 16:10 KST 평일. range 1M 이 실제론 ~1년치라 증분·공백 self-heal 충분.
    SeedJob(
        id="job_worker.collect_vkospi",
        owner="job_worker",
        handler_key="collect_vkospi",
        cron="10 16 * * 1-5",
        kwargs={"range_token": "1M"},
    ),
    # 매크로 게이트 입력 (2026-06-24) — 시장전체 투자자 수급(연기금 분리, 네이버).
    # 장후 수급 확정 후 18:40 KST 평일 (종목별 collect_investor_trading 30 18 다음).
    SeedJob(
        id="job_worker.collect_market_investor_flows",
        owner="job_worker",
        handler_key="collect_market_investor_flows",
        cron="40 18 * * 1-5",
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
    # Track B — weekly_factor_analysis (v2 utility_jobs_dag: 0 22 * * 5, 주간 금요일 밤).
    SeedJob(
        id="job_worker.weekly_factor_analysis",
        owner="job_worker",
        handler_key="weekly_factor_analysis",
        cron="0 22 * * 5",
        kwargs={"period_days": 30},
    ),
    # Track B — collect_minute_chart (v2 utility_jobs_dag: */5 9-15 * * 1-5).
    # KIS 분봉의 단일 수집자 (KOSPI 시총 top200 + 측정 대기 시트). 2026-06-12
    # 30→200 확대 (KOSPI 200 상당 — "데이터는 많이 모을수록 좋다"). 같은 날 도입한
    # 적응형 페이싱이 240초 창에 펴므로 5분 주기 안에서 KIS 한도와 충돌 없음.
    # 운영 DB 는 같은 날 UPDATE 적용 — 이 시드는 새 환경 재현용.
    SeedJob(
        id="job_worker.collect_minute_chart",
        owner="job_worker",
        handler_key="collect_minute_chart",
        cron="*/5 9-15 * * 1-5",
        kwargs={"top_n": 200},
    ),
    # Track B — collect_full_market_data (v2 utility_jobs_dag: 0 16 * * 1-5, 일봉 top300).
    # 운영 일봉 수집자 — stock_masters 시총 top300 자동발견, 18 req/s 페이싱, idempotent
    # upsert. 2026-05-22 운영 전환 (enabled): 시드 placeholder 로 disabled 상태가 승격
    # 누락돼 daily_prices 가 04-17 에 동결됐던 것을 발견 → 활성화. days=45 는 결정 선정
    # 코어의 ≥20 거래일 윈도우 + 마진 + 소규모 공백 self-heal 용.
    SeedJob(
        id="job_worker.collect_full_market_data",
        owner="job_worker",
        handler_key="collect_full_market_data",
        cron="0 16 * * 1-5",
        kwargs={"top_n": 300, "days": 45},
    ),
    # Track B — daily_briefing_report (v2 utility_jobs_dag: 0 17 * * 1-5, 장마감 후 브리핑).
    # Telegram 전송은 TELEGRAM_BOT_TOKEN/CHAT_ID 가 설정되어야 실제 발송. 기본은 skip.
    SeedJob(
        id="job_worker.daily_briefing_report",
        owner="job_worker",
        handler_key="daily_briefing_report",
        cron="0 17 * * 1-5",
        kwargs={},
    ),
    # paper_outcomes — STOP 상태에서 발행된 시트의 사후 paper PnL 측정 (2026-05-27 도입).
    # daily_prices 가 16:00 갱신 후 충분 마진을 두고 18:00 KST 평일 시작. 윈도우(시트의
    # time_stop hold_days) 가 닫힌 미측정 시트들 일괄 시뮬레이션 → paper_outcomes 적재.
    # v0 은 일봉 simulator (overextension_exit 자동 skip), v1 에서 1분봉 어댑터 + scale_out
    # 정밀 처리. control STOP 무관 — 시트는 STOP 중에도 DB 에 영속되므로 측정 대상으로 충분.
    SeedJob(
        id="job_worker.paper_outcomes_daily",
        owner="job_worker",
        handler_key="paper_outcomes_daily",
        cron="0 18 * * 1-5",
        kwargs={},
    ),
    # Phase 2.13-1 — WSJ/Bloomberg/Reuters RSS 크롤러. 2시간 간격.
    SeedJob(
        id="job_worker.global_news_crawl",
        owner="job_worker",
        handler_key="global_news_crawl",
        cron="0 */2 * * *",
        kwargs={},
    ),
    # Phase 2.13-1 — 일일 digest (LLM 요약). 08:00 개장 전 + 12:30 점심, 평일.
    # Macro Council cron (40 7,11 * * 1-5) 보다 약간 앞서 돌아 fresh digest 를 남긴다.
    SeedJob(
        id="job_worker.global_news_digest",
        owner="job_worker",
        handler_key="global_news_digest",
        cron="30 7,11 * * 1-5",
        kwargs={"lookback_hours": 24},
    ),
    # WSJ 뉴스레터 4종 Gmail 수집 + Telegram 요약 (v2 council_trigger 포팅).
    # global_macro_news_articles 에 source='wsj_newsletter' 로 upsert → 07:30
    # global_news_digest 가 자동 흡수 → 08:30 첫 Macro run 에 반영.
    SeedJob(
        id="job_worker.wsj_gmail_ingest",
        owner="job_worker",
        handler_key="wsj_gmail_ingest",
        cron="20 7 * * 1-5",
        kwargs={},
    ),
    # 사용자 지정 종목 buy-only 분할매수 tick (user_directed_dca). 매 영업일 장중 매 분.
    # 활성 캠페인이 없으면 즉시 no-op — 텔레그램 /dca arm 으로 무장돼야 일을 한다.
    # 매 분인 이유: 급락 가속을 조기 감지하고 VI 5분 재시도를 제때 집어야 하기 때문.
    SeedJob(
        id="job_worker.user_directed_dca_tick",
        owner="job_worker",
        handler_key="user_directed_dca_tick",
        cron="* 9-15 * * 1-5",
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
