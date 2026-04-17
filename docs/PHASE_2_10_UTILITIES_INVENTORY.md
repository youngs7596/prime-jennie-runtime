# Phase 2.10 — v2 utilities/scripts/prompts/reports 편입 인벤토리

> Track E — utilities 편입 (#9). 2026-04-17 초안.
>
> 판정: **port** (v3 로 이관), **skip** (v3 이미 커버), **obsolete** (일회성/연구), **defer** (다른 Track blocker).

## 1. `prime-jennie/utilities/` — **empty**

v2 에도 비어있음. 아무것도 이관하지 않는다. v3 최상위 `utilities/` 디렉토리 생성도 불필요.

## 2. `prime-jennie/scripts/` — 26 파일 (5,751줄)

| v2 파일 | 줄수 | 판정 | 비고 / 이관 대상 |
|---|---:|---|---|
| `analyze_gap_up.py` | 388 | **defer → #8 (backtest)** | backtest 도메인 분석 도구. v3 `backtest/` 포팅 시 같이. |
| `analyze_us_kospi_correlation.py` | 489 | **obsolete** | 1회성 연구. 결과물 `reports/` CSV 로 남아있음. |
| `backfill_macro_flows.py` | 57 | **obsolete** | MariaDB 1회성 backfill (off-by-one 버그 정정). v3 는 해당 버그 없음. |
| `backtest_overextension.py` | 299 | **defer → #8** | backtest 도메인. |
| `cleanup_old_data.py` | 59 | **port** | 90일 초과 데이터 삭제. v3 Postgres + scheduled_jobs 로 이관. Track A(#2) 스키마 확정 후 실제 포팅 — **blocker #2**. |
| `daily_asset_snapshot.py` | 74 | **defer → #1 (job-worker)** | 15:45 KST daily snapshot. job-worker 의 `sync-balances` / `asset-snapshot` 엔드포인트 범주. |
| `fix_trade_logs_profit.py` | 134 | **obsolete** | MariaDB NULL 필드 1회성 복구. v3 는 스키마 다름. |
| `grid_search_overextension.py` | 577 | **obsolete** | 연구용 그리드서치. 결론은 v2 production 에 이미 반영됨. |
| `grid_search_sw_fine.py` | 213 | **obsolete** | 동상. |
| `migrate_from_legacy.py` | 443 | **obsolete** | my-prime-jennie → prime-jennie 1회성 MariaDB 마이그. 이미 완료. |
| `mine_signals.py` | 738 | **obsolete** | 데이터마이닝 연구. v3 scout 은 code generation. |
| `poc_concurrency_test.py` | 194 | **obsolete** | DeepSeek 동시성 PoC. 결과 반영 끝. |
| `poc_vllm_throughput.py` | 177 | **obsolete** | vLLM PoC. 결과 반영 끝. |
| `reanalyze_sentiment.py` | 214 | **obsolete** | 2026-02~03 score=50 버그 1회성 재분석. |
| `reanalyze_standalone.py` | 276 | **obsolete** | 동상. |
| `run_backtest.py` | 158 | **defer → #8** | backtest CLI entry. |
| `seed_stock_masters.py` | 159 | **defer → #2** | stock_masters Postgres 스키마 확정 후 port. |
| `sweep_rsi_threshold.py` | 324 | **obsolete** | 연구용 스윕. |
| `sync_positions.py` | 152 | **defer → #1** | KIS ↔ DB 포지션 동기화. job-worker 소관. |
| `update_naver_sectors.py` | 65 | **defer → #2** | Naver 섹터 매핑 크롤. Postgres stock_masters 확정 후. |
| `update_stock_master.py` | 48 | **defer → #2** | KIS 전 종목 갱신. 상동. |
| `install.sh` | 194 | **obsolete** | v2 MariaDB+Airflow 기반 설치. v3 는 uv + docker compose. |
| `setup_dev.sh` | 194 | **obsolete** | v2 MariaDB `jennie_db_dev` 테스트베드. |
| `setup_dev_views.sql` | 62 | **obsolete** | MariaDB VIEW. |
| `systemd_autostart.sh` | 36 | **port** | GPU-aware systemd 부팅 스크립트. v3 compose 용으로 재작성 → `scripts/systemd_autostart.sh` (#11 에서 마무리). |
| `airflow-entrypoint.sh` | 27 | **obsolete** | v3 는 apscheduler + scheduled_jobs. Airflow 미사용. |

**요약**: port 2 / defer 8 / obsolete 16.

## 3. `prime-jennie/prompts/` — 7 파일

| v2 파일 | 줄수 | 판정 | 이관 대상 |
|---|---:|---|---|
| `analyst/unified_analyst.txt` | 52 | **obsolete** | v2 Scout 정량+LLM 최종 스코어링. v3 Scout 은 Python code generation 이라 대체됨. |
| `news/unified_analysis.txt` | 105 | **defer → #1** | 감성+경쟁사 리스크 동시 분석. v3 news_pipeline_kor 는 감성만 처리 — 경쟁사 리스크는 job-worker 포팅 시 사용. |
| `news/sentiment.txt` | 17 | **skip** | v3 `news_pipeline_kor/adapters/exaone_sentiment.py` 가 자체 inline 프롬프트로 대체. |
| `briefing/daily_briefing.txt` | 64 | **port → #6** | briefing 서비스 그대로 사용. `prompts/briefing/daily_briefing.txt` 이관. |
| `council/macro_strategist.txt` | 40 | **port → #7** | council 로깅에서 원형 유지. |
| `council/macro_risk_analyst.txt` | 38 | **port → #7** | 상동. |
| `council/macro_chief_judge.txt` | 48 | **port → #7** | 상동. |

**요약**: port 4 / defer 1 / skip 1 / obsolete 1.

## 4. `prime-jennie/reports/` — 4 CSV

| v2 파일 | 판정 | 비고 |
|---|---|---|
| `crash_precursor_2026-04-05.csv` | **archive** | `docs/archive/v2_reports/` |
| `us_kr_cross_correlation_2026-04-05.csv` | **archive** | 상동 |
| `us_kr_gap_correlation_2026-04-05.csv` | **archive** | 상동 |
| `vix_kospi_impact_2026-04-05.csv` | **archive** | 상동 |

전부 1회성 연구 결과물. 런타임 자산 아님 → 참고용 보관.

## 5. 이 PR 에서 수행

- [x] `prompts/briefing/daily_briefing.txt` 이관
- [x] `prompts/council/macro_strategist.txt` 이관
- [x] `prompts/council/macro_risk_analyst.txt` 이관
- [x] `prompts/council/macro_chief_judge.txt` 이관
- [x] `docs/archive/v2_reports/*.csv` 이관
- [x] 인벤토리 문서 (이 파일)

**차후 (blocker 해소 시)**:
- `cleanup_old_data.py` port — #2 Postgres 스키마 확정 후
- `systemd_autostart.sh` port — #11 compose 통합 마무리 단계
- `sync_positions.py` / `daily_asset_snapshot.py` — #1 job-worker 포팅 시
- `seed_stock_masters.py` / `update_naver_sectors.py` / `update_stock_master.py` — #2 stock_masters 테이블 확정 후
- `news/unified_analysis.txt` — #1 경쟁사 리스크 분석 엔드포인트 포팅 시
- backtest 계열 스크립트 — #8
