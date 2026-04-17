# jobs/ — v2 job-worker 포팅 영역 (Track B)

v2 `prime_jennie/services/jobs/app.py` (2883줄, 26 ep) 중 **22개** 를 포팅한다.
나머지 4개 (news_pipeline.crawl_cycle, price_scheduler.collect_minute/daily,
slow_loop.scout_daily) 는 이미 전용 runner 로 분리됨.

## 원칙

- **재작성 금지.** v2 구조/분기/임계값을 그대로 유지하고 v3 async + asyncpg
  어댑터만 씌운다. v2 에서 안정적이었던 파라미터를 재해석하지 않는다.
- **apscheduler handler** = `async def handler(**kwargs) -> None`. `JobResult`
  BaseModel 은 반환하지 않는다 (runner 가 DB 상태만 기록).
- 각 job 은 **명시적 의존 주입** — 모듈 전역 싱글턴 금지. runner 에서
  `asyncpg.Pool` + `httpx.AsyncClient` 를 파라미터로 받아 클로저로 등록.

## 파일 구조

| 파일 | 담당 job |
|------|---------|
| `maintenance.py` | cleanup_old_data, update_naver_sectors, seed_stock_masters, contract_smoke_test |
| `market_data.py` | collect_index_daily_prices, collect_us_market, collect_investor_trading, collect_foreign_holding, refresh_market_caps, collect_minute_chart (백테스트 상위30) |
| `fundamentals.py` | collect_dart_filings, collect_consensus, collect_naver_roe, collect_quarterly_financials |
| `analytics.py` | daily_asset_snapshot, analyze_ai_performance, analyst_feedback, weekly_factor_analysis |
| `council_macro.py` | macro_collect_global/korea, macro_validate_store, macro_quick, council_trigger, council_insight |
| `positions.py` | sync_positions |
| `briefing_glue.py` | daily_briefing_report (호출만 — 구현은 Track D briefing) |

## 의존성

- **DB**: Track A (`track-a-db`) 가 migrations 006+ 로 v2 MariaDB → Postgres
  포팅 중. 필요한 테이블 누락 시 DM.
- **v3 기존 테이블**: `daily_prices`, `minute_prices` (003) — v2 의
  `stock_daily_prices`/`stock_minute_prices` 대체.
- **크롤러**: `crawlers/` 서브 패키지에 v2 `prime_jennie.infra.crawlers.*` 포팅
  (fnguide/naver_market/us_market 등). news 관련은 Track E `news_pipeline_kor/`
  재사용.
- **briefing**: Track D `prime_jennie_runtime/briefing/` 사용. 완료 전에는
  `briefing_glue.py` 에 TODO placeholder 유지.
- **council**: Track D `prime_jennie_runtime/council_logging/` 와 조율.

## 스케줄 시드

`scripts/seed_scheduled_jobs.py` 에 owner=`job_worker` job 을 추가한다. cron 은
v2 `dags/utility_jobs_dag.py` 의 default_args + schedule_interval 과 맞춘다.

## 테스트

`tests/jobs/` 에 도메인 별 happy path smoke test. 외부 HTTP 는 `respx` 로
mock, DB 는 asyncpg 에 fakes 또는 test DB 접근(conftest).
