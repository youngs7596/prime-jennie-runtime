# v2 Teardown — 오케스트레이션 · DB · 실데이터

작성: orchestration 에이전트 / 2026-05-22 / v2-teardown 팀
대상: prime-jennie (v2) — `/home/youngs75/projects/prime-jennie`, 마지막 커밋 2026-04-21, 2026-04-18 퇴역
원칙: 코드·데이터가 진실. file:line 인용. 못 알아낸 건 명시.

## 0. 조사 방법 / 데이터 출처

- v2 MariaDB(`jennie_db`) 부활: `docker compose --profile legacy up -d mariadb` (MS-01, port 3307, container `prime-jennie-mariadb-1`). healthy 확인. 데이터 잔존 197M / 111 테이블 (Airflow 메타테이블 포함).
- 실데이터 = 위 DB 직접 쿼리. 코드 = v2 repo 직접 read.
- DB 주의: `jennie_db` 에 (a) v2 앱 테이블(복수형, migration 001~011 정의) (b) v1 잔존 테이블(단수형) (c) Airflow 메타테이블이 섞여 있음. 본 문서는 어느 쪽인지 매번 구분 표기.

---

## 1. v2가 한 일 · 메커니즘 (file:line)

v2 오케스트레이션은 **물리적으로 분리된 2개 시스템**이다. 이 분리 자체가 v2 설계의 핵심.

### 1.1 시스템 A — Airflow (배치 스케줄링)

- 구성: `airflow-webserver`(api-server, :8085) + `airflow-scheduler`(LocalExecutor), 메타DB = MariaDB `jennie_db` 공유 (`docker-compose.yml:355-406`).
- DAG 파일은 **4개뿐**, 그 안에 **22개 DAG** 정의:
  - `dags/scout_job_dag.py` — `scout_job_v1` 1개
  - `dags/macro_dag.py` — `enhanced_macro_collection`, `macro_council`, `enhanced_macro_quick` 3개
  - `dags/utility_jobs_dag.py` — `_utility_dag()` 팩토리(`utility_jobs_dag.py:16-46`)로 데이터수집/정리/분석 DAG 양산. 코드상 19개 정의.
- **모든 DAG 은 비즈니스 로직 0줄**. `HttpOperator` 로 서비스 엔드포인트에 POST 만 한다. 예: `scout_job_dag.py:25-34` 는 `scout-job:8087/trigger` 호출, `response_check=lambda resp: resp.status_code == 200`. 실제 로직은 전부 `scout-job`/`job-worker` 서비스 안에 있음.
- 스케줄 분포(실DB `dag` 테이블 + cron):
  - 장중 고빈도: `enhanced_macro_quick`(`*/5 9-15`, intraday risk throttle), `collect_minute_chart`(`*/5 9-15`)
  - 장중 시간별: `scout_job_v1`(`30 8-14`, 7회/일)
  - 매크로: `enhanced_macro_collection`/`macro_council`(`7,11시`)
  - 일배치(장후): asset snapshot → market cap → 일봉 → 지수 → 브리핑 → 수급/외국인/DART
  - 주/월/분기: factor 분석, 네이버 섹터, ROE, 컨센서스, 분기재무
- DAG 의존성은 거의 없음. 유일한 fan-in: `enhanced_macro_collection` 의 `[collect_global, collect_korea] >> validate_and_store` (`macro_dag.py:58`).
- 공통화: `dags/airflow_utils.py` — `get_default_args()` 가 모든 DAG 에 `on_failure_callback=send_telegram_alert` 주입(`airflow_utils.py:49-53`). 실패 시 텔레그램 자동 알림.
- 부트스트랩: `scripts/airflow-entrypoint.sh` — 컨테이너 기동마다 `airflow db migrate` + HTTP Connection 4개(`scout_job`/`job_worker`/`macro_council`/`price_monitor`)를 delete+add 멱등 패턴으로 등록. DB `connection` 테이블에서 4개 확인.

### 1.2 시스템 B — Redis Streams (실시간 매매 루프)

Airflow 는 매매를 **전혀** 오케스트레이션하지 않는다. 실시간 매매는 별도 stream 파이프라인:

- `scanner/app.py:1-10` 의 Data Flow 주석: `Gateway KIS WebSocket → Redis kis:prices → Scanner (XREADGROUP) → Redis stream:buy-signals → Executor`.
- 즉 `kis-gateway`(:8080) 가 KIS WebSocket 틱을 Redis 에 publish → `buy-scanner`(:8081) 가 consumer group 으로 소비, 시그널 감지 → `stream:buy-signals` 발행 → `buy-executor`(:8082) 소비·주문. 매도는 `price-monitor`(:8088)/`sell-executor`(:8083).
- 이 서비스들은 Airflow task 가 아니라 **상주 stream consumer**. (scanner/monitor/buyer/seller 에서 apscheduler·cron 패턴 grep 결과 없음 — XREADGROUP 블로킹 소비 구조.)

### 1.3 긴급정지(kill-switch) 메커니즘

- Redis 단일 플래그 `trading_flags:stop` 1개. scanner/buyer/seller/monitor **4개 서비스가 각자 독립적으로** 이 키를 읽어 행동을 멈춤 (`scanner/app.py:47,478`, `buyer/executor.py:39,445-448`, `seller/executor.py:28,300-302`, `monitor/app.py:60,267`).
- 세팅 주체: 운영자 — 텔레그램 `/stop 확인` 명령 (`telegram/handler.py:241-245`). 2단계 확인("확인"/"긴급" 인자 없으면 거부). `/resume` 로 해제(`handler.py:237-238`).
- seller 는 `if not is_manual and self._is_emergency_stopped()` (`seller/executor.py:129`) — STOP 중에도 **수동 매도는 허용**. 운영자가 직접 청산 가능하도록 설계.

### 1.4 데이터 모델 (migrations)

- Alembic 마이그레이션 11개(`migrations/versions/001~011`), app 스키마는 `011` 까지 적용(실DB `alembic_version_app` = `011`). Airflow 자체 스키마는 `alembic_version` = `509b94a1042d` 로 분리.
- `001_initial_schema.py` — v2 핵심 13테이블 동시 생성(Create Date 2026-02-19): `stock_masters`/`configs`/`stock_daily_prices`/`stock_investor_tradings`/`stock_fundamentals`/`daily_quant_scores`/`stock_news_sentiments`/`daily_macro_insights`/`global_macro_snapshots`/`positions`/`trade_logs`/`daily_asset_snapshots`/`watchlist_histories`. FK 는 전부 `stock_masters.stock_code` 기준 star schema.
- 이후 002~011 은 점진적 증분(공시·분봉·컨센서스·지수일봉·미국시장·signal_logs 추가, 컬럼 보강).
- `008_add_run_id_to_history_tables.py` — `daily_quant_scores`/`watchlist_histories` 에 `run_id`+`is_active` 추가. PK/UQ 를 `(date, code)` → `(date, code, run_id)` 로 변경. 하루 7회 도는 scout 의 매 실행을 보존하되 "그날 마지막 운영 실행"만 `is_active=1`. (스키마 주석 `008:3-6`)
- `010_add_signal_logs.py` — Create Date 2026-03-06. 주석 그대로: *"Stop 상태에서도 발생한 매수/매도 시그널을 DB에 기록하여 나중에 백테스트 데이터로 활용."* 컬럼 `status`(suppressed/published), `suppressed_reason`(stop/pause).

---

## 2. v2가 잘한 것 (핵심)

증거 기반으로, v2 오케스트레이션·데이터 계층에서 **실제로 견고했던** 것들:

### 2.1 "스케줄러는 트리거만, 로직은 서비스에" — 깔끔한 책임 분리

DAG 22개 전부가 `HttpOperator` 한 줄. Airflow 는 cron + 재시도 + 알림만 담당하고, 비즈니스 로직은 0줄. 효과:
- DAG 코드가 거의 변하지 않음 → Airflow 가 거대 진실원천이 되지 않음.
- 같은 작업을 Airflow 없이도 `curl` 로 재현 가능(엔드포인트가 곧 인터페이스).
- `_utility_dag()` 팩토리 1개로 19개 DAG 을 선언적으로 양산 — 신규 배치 추가 비용이 5줄.

### 2.2 운영 안정성 — 실데이터가 증명

실DB `dag_run`/`task_instance` 집계 (운영 기간 2026-02-19~04-17, ~2개월):

| 항목 | 값 |
|---|---|
| 총 dag_run | 6,910 (22 DAG, GROUP BY 합산) |
| **최종 실패 dag_run** | **0** |
| task_instance | 7,097 success / 1 NULL / 실패 0 |
| 재시도로 살아난 task | 52건 (try2=49, try3=3) — 전부 최종 성공 |
| 최다 실행 DAG | `collect_minute_chart` 3,321회 · `enhanced_macro_quick` 2,615회 — 모두 100% |

2개월간 ~6,900 스케줄 실행 / ~7,100 task 에서 최종 실패 0. 재시도(`retries=1~2`)가 transient 실패 52건을 전부 흡수. 이건 "운이 좋았다"가 아니라 **재시도 정책 + 멱등 작업 설계 + 알림**이 맞물려 돌아간 결과.

### 2.3 작업 크기에 맞춘 스케줄 — 과부하 없음

실DB task duration 평균:
- 무거운 작업은 무겁게: `scout_job_v1` 평균 590초(max 2,214초≈37분), execution_timeout 60분으로 여유 확보.
- 가벼운 작업은 가볍게: `enhanced_macro_quick` 평균 2.2초 — 5분 간격 intraday 작업에 적정. `daily_index_prices` 0.4초.
- `enhanced_macro_quick` 는 `max_active_runs=1`(`macro_dag.py:92`)로 5분 간격 누적 폭주 방지.

타임아웃·간격·max_active_runs 가 실제 작업 비용에 맞게 튜닝돼 있음.

### 2.4 `run_id` 이력 모델 — 재실행을 1급 시민으로

scout 가 하루 7회 돈다 → 같은 날짜에 다중 결과가 정상. v2 는 이걸 PK 충돌 회피로 때우지 않고 `008` 에서 `run_id` 차원을 추가하고 `is_active` 로 "그날의 canonical 실행"을 명시(`migrations/008`). 실DB 확인: `daily_quant_scores` 20,996행 / `run_id` 161개(≈7/일×22일). **모든 중간 실행이 보존되면서 "현재 뷰"가 모호하지 않다** — 재현성과 운영뷰를 동시에 만족.

### 2.5 STOP 을 "관측 모드"로 설계 — signal_logs

`010` 의 설계 의도가 핵심이다. 2026-03-06 운영자가 긴급정지를 걸 때, 동시에 `signal_logs` 테이블을 만들어 **STOP 중에도 생성되는 시그널을 전량 적재**. 실DB: `signal_logs` 408,941행, 전부 `status=suppressed` / `suppressed_reason=emergency_stop`, 2026-03-06~04-17. STOP 이 "포기"가 아니라 "라이브 차단 + 섀도 데이터 수집 계속"으로 설계됨 — 백테스트 자산을 6주간 적립.

### 2.6 단순·견고한 kill-switch

Redis 키 1개를 4개 서비스가 각자 읽는 구조(2.3절 §1.3). 중앙 코디네이터 없이도 "한 곳 끄면 전부 멈춤"이 보장되고, 2단계 확인 + STOP 중 수동매도 허용까지 갖춤. 분산 시스템 긴급정지치고 군더더기가 없다.

---

## 3. v2가 못한 것 (간략 · 증거)

### 3.1 Airflow "성공" ≠ 작업 정상

`response_check` 가 **HTTP 200 여부만** 검사(`scout_job_dag.py:32` 등 전 DAG 동일). 서비스가 200 만 주면 내부에서 아무 일 안 해도 "success". 실증거: `daily_briefing_report` DAG 은 44회 100% success 인데, `telegram_briefings` 테이블은 2026-02-18 에서 멈춤(13행). 브리핑 산출물이 끊겼는데 Airflow 는 6주간 초록불. (테이블이 폐기됐을 수도 — 단정 불가하나, "성공"이 산출물을 검증하지 못한다는 구조적 공백은 확실.)

### 3.2 코드 ↔ 런타임 드리프트

`utility_jobs_dag.py:146` 에 `collect_us_market` DAG 정의가 있으나 실DB `dag` 테이블엔 미등록(22개 중 부재, `import_error` 테이블도 0행). repo 에 있으나 한 번도 등록·실행 안 된 DAG. `us_market_daily` 데이터(2,513행)는 다른 경로(스크립트/수동 job 호출)로 적재된 것으로 보임. 배포 갭 또는 DB 스냅샷 이후 추가 — orchestration 데이터만으론 원인 단정 불가.

### 3.3 메타DB 동거 + 레거시 테이블 미정리

- Airflow 메타데이터가 앱 DB(`jennie_db`)에 동거 → 111 테이블 중 절반이 Airflow 것. 백업·마이그레이션·용량 분석이 섞임.
- v1 단수형 테이블 다수 잔존(`stock_master` 1,028 / `daily_quant_score` 0 / `tradelog` 371 / `config` 32 / `shadow_radar_log` 16,800 / `llm_decision_ledger` 16,768 …). `scripts/migrate_from_legacy.py` 로 v2 복수형 테이블에 ETL 후 **원본을 drop 안 함**. 단·복수 동명 테이블이 "어느 게 진짜냐"를 매 쿼리 헷갈리게 함.

### 3.4 v2 신아키텍처의 라이브 실적 표본이 매우 얇다 — 가장 중요

실데이터 타임라인:
- v1→v2 스키마 컷오버: **2026-02-19** (`001` Create Date, v1 테이블 `llm_decision_ledger`/`shadow_radar_log` 마지막 기록일 모두 02-19).
- v2 신아키텍처 라이브 매매: **2026-02-19 ~ 03-05, 약 2주**.
- 2026-03-06 운영자 `/stop` → `signal_logs` 전량 `emergency_stop`. 이후 retire(04-18)까지 **6주간 published 시그널 0건, 실거래 0건**.
- 그래서 `trade_logs`(434행, BUY 마지막 03-03 / SELL 마지막 03-21)의 대부분은 **v1 시절(2025-11~) 거래**가 ETL 로 넘어온 것. v2 신아키텍처가 라이브로 만든 실적은 ~2주치뿐.

→ "v2가 v3보다 나았다"는 인상은 **v2 신아키텍처를 ~2주만 라이브로 본 표본**에 근거할 위험이 있음. **§5 에서 데이터로 확정** — v2 stream 아키텍처 라이브 거래는 61건(실청산 29건)·합산 −34만원에 불과하고, +30.5M/승률70% 실적은 전부 v1-ETL.

---

## 4. v3 비교 훅 (2단계 점검 항목)

2단계에서 "v3가 v2 대비 유지/개선/퇴보시켰나"를 볼 구체 체크리스트:

1. **스케줄링 책임 분리**: v2 = Airflow(트리거)+서비스(로직) 깔끔 분리. v3 는 스케줄·트리거 계층이 무엇인가? v3 에 Airflow 가 남았나, slow_loop/fast_loop/cron 으로 대체됐나? 22개 배치(데이터수집·scout·macro·브리핑)가 v3 어디서 도는가 — 전수 매핑하고 누락 DAG 확인.

2. **운영 안정성 기준선**: v2 = 2개월 8천+ 실행 최종실패 0. v3 의 동등 기간 스케줄 실패율은? (v3 의 dag_run 등가물 — cron 로그/run 테이블 — 집계해 비교.)

3. **"성공" 검증 깊이**: v2 의 약점은 HTTP 200=success. v3 는 작업 산출물(레코드 적재 등)까지 검증하나, 아니면 같은 공백을 답습했나? (§3.1 의 briefing 같은 silent degradation 재발 위험.)

4. **재실행/이력 모델**: v2 `run_id`+`is_active` 패턴(§2.4)이 v3 scout/screening 산출에도 있나? MEMORY 의 `persist_scout_run`→`persist_screening_candidates` FK 순서·`reconcile` drift 이슈가 이 모델 부재/약화 때문인지 점검.

5. **실시간 매매 파이프라인**: v2 = Redis Streams + consumer group(`stream:buy-signals`, XREADGROUP). v3 도 Redis Streams 라면 **stream 아키텍처는 v2 발명이지 v3 신규가 아님** — v3 의 순수 기여(Agent Coordinator/Harness)가 이 위에 무엇을 더했는지, 그게 §3 의 "외톨이 패턴"을 실제로 줄였는지 검증. (MEMORY: `project_v3_orchestrator_decision`, `project_audit_2026_05_14`.)

6. **Kill-switch**: v2 = Redis `trading_flags:stop` 1키 + 4서비스 독립 polling + 텔레그램 2단계확인 + STOP중 수동매도 허용. v3 의 긴급정지는 동등 이상인가? STOP 중에도 reconcile/관측이 도는가(v2 signal_logs 패턴 계승 여부).

7. **데이터 모델 위생**: v2 = 앱DB+Airflow DB 동거, v1 레거시 테이블 미정리. v3(postgres `prime_jennie_v3`)는 분리·정리됐나? MEMORY 의 `daily_macro_insights v3 dead`(v2 잔존 테이블) 같은 미정리가 v3 에도 누적 중인가.

8. **STOP 표본 함정 (가장 주의)**: v2 신아키텍처 라이브 표본 ~2주(§3.4). v3 평가 시 "v2가 잘했다"의 근거가 이 2주인지, 아니면 v1 ETL 거래까지 섞은 `trade_logs` 인지를 반드시 분리. v3 1단계 평가에서 동일 함정(짧은 윈도우·STOP 차단 표본) 회피 — MEMORY `single_day_overfit`, `thesis_gate_deferred` 와 직결.

---

## 5. trade_logs era 분할 — v1-ETL vs v2-native (확정)

team-lead 지시로 `trade_logs` 437행 전체를 2026-02-19 v1→v2 컷오버 기준 분할. execution §1.7 성과(trailing 25건 중 23승 등)와 발견 #3 의 충돌을 데이터로 확정한다.

### 5.1 분할 방법 (era 구분 컬럼 부재 → id/timestamp 기반)

`trade_logs` 에 era/source/origin 컬럼 없음(`SHOW COLUMNS` 확인). 다음 흔적으로 경계 확정:
- 레거시 `tradelog`(v1, 단수형) = 371행, 2025-11-05~**2026-02-19 02:21:02**.
- `trade_logs` id 1–370 = 시각이 모두 실제 HH:MM:SS, 타임스탬프 오름차순, id 370 = 2026-02-19 02:21:02 (레거시 max 와 정확히 일치). → **id 1–370 = v1-ETL** (`scripts/migrate_from_legacy.py` 가 레거시 `tradelog` 을 복사. 371→370 은 다른 ETL 함수들과 동일한 `WHERE stock_code IN (stock_masters)` FK 필터로 1행 탈락 추정).
- id 371–376 = 6행 모두 `trade_timestamp` = `2026-02-20 00:00:00` (시각 00:00:00 = date-only 배치 시그니처). 레거시 max 이후 → v2 가 쓴 것이나 **실시간 실행 경로 아님**(라이브 경로는 id 377+ SELL 처럼 실 시각을 찍음). 초기 포지션 시딩으로 추정 — 정확한 메커니즘 미확정.
- id 377–437 = 실 HH:MM:SS → **v2 stream 아키텍처 실거래**.

### 5.2 era 분할표

| era | id 범위 | BUY | SELL | 합 | 기간 | 성격 |
|---|---|---|---|---|---|---|
| **v1-ETL** | 1–370 | 160 | 210 | 370 | 2025-11-05~02-19 | 레거시 `tradelog` ETL 이관분 |
| **v2-native seed** | 371–376 | 6 | 0 | 6 | 02-20 00:00:00 | v2 작성, date-only, 라이브 경로 아님 |
| **v2-native live** | 377–437 | 26 | 35 | 61 | 02-20~03-21 | **v2 stream 아키텍처 실거래** |

→ **v2 의 실시간 stream 아키텍처가 라이브로 만든 거래 = 총 61건 (BUY 26 / SELL 35)**, 전부 2026-02-20~03-04 (+ 03-21 마지막 1건). 청산 완결(SELL) 35건.

### 5.3 청산사유 분석 (trade_logs SELL)

**[정정]** 초판은 `strategy_signal` 컬럼으로 분류했으나, SELL 행의 청산사유는 `reason` 컬럼에 들어간다. v2 seller 는 SELL 시 `reason` 에 청산 enum 을, `strategy_signal` 에는 그 종목의 *진입* 전략을 역참조해 넣는다(`seller/app.py` `_persist_sell` — execution 지적 + 본인 재쿼리로 확인). v1-ETL 행은 `reason` == `strategy_signal` (같은 값)이라 초판 v1 수치는 그대로 유효.

v2-native LIVE SELL 35건을 `reason` enum 으로 분류 (id 377–437):

| reason | 건수 | 승 | 패 | 손익 NULL |
|---|---|---|---|---|
| TRAILING_STOP | 7 | 7 | 0 | 0 |
| PROFIT_TARGET | 7 | 7 | 0 | 0 |
| STOP_LOSS | 6 | 0 | 6 | 0 |
| DEATH_CROSS | 3 | 0 | 3 | 0 |
| MANUAL | 6 | 2 | 4 | 0 |
| MANUAL_SYNC | 6 | – | – | 6 |
| **합** | **35** | **16** | **13** | **6** |

- **MANUAL_SYNC 6건은 포지션 동기화 작업이지 실청산이 아니다**(profit 전부 NULL — 초판의 "6 손익미기록"이 이것). → v2 stream 아키텍처의 실제 청산 = **29건**.
- 익절형(TRAILING_STOP 7 + PROFIT_TARGET 7 = 14)은 전부 수익, 손절형(STOP_LOSS 6 + DEATH_CROSS 3 = 9)은 전부 손실(exit type 정의상 당연). STOP_LOSS 6건은 profit_pct −5.0~−5.3% 군집(id 415·416·424·425·428·429)으로 5% 고정손절 실발동 확인, 최대손실 id 416 −2.40M.
- v1-ETL SELL 210건은 `reason`(=strategy_signal) 이 verbose 자유텍스트("Trailing TP: High…","Scale-out L1…","Fixed Stop Loss…"). 카테고리 환산: manual 69 / stop_loss 46 / system_auto_exit 39 / scale_out 38 / trailing 18.

→ 청산 taxonomy 는 **두 era 모두 분석 가능**하다(초판의 "v2 는 청산사유 미기록"은 컬럼 오독 — 정정). 다만 v1 은 자유텍스트, v2 는 6-값 enum 으로 **기록 형식이 바뀌었다**. execution §1.7 의 v1/v2 혼재 우려는 era-split 으로 해소됨(execution 이 §1.7 재작성).

### 5.4 성과 × era

| 지표 | v1-ETL | v2-native (live) |
|---|---|---|
| SELL 행 수 | 210 | 35 (실청산 29 + MANUAL_SYNC 6) |
| 승 / 패 / 손익 NULL | 141 / 60 / 9 | 16 / 13 / 6 |
| 승률(기록분 기준) | 70.1% | 55.2% |
| 평균 profit_pct | +2.72% | +0.97% |
| **합산 profit_amount** | **+30,535,586 KRW** | **−344,722 KRW** |
| 평균 holding_days | 2.94일 | 미기록(NULL) — v2 가 채우지 않음 |

**확정 결론:** v2 의 "좋아 보이는 실적"(+30.5M / 승률 70% / 평균 +2.72%)은 **100% v1-ETL(2026-02-19 이전 레거시 거래)**다. v2 stream 아키텍처가 라이브로 만든 표본은 **실청산 29건, 합산 −34만원(사실상 손익분기~소폭 마이너스), 승률 55%**. 표본 기간 ~2주(02-20~03-04). 이 표본으로 "v2 아키텍처가 우수했다"를 주장할 근거는 없다 — 통계적으로 너무 얇고 결과도 플러스가 아니다. (execution 도 이 방향에 동의, §1.7 을 era-split 으로 재작성.)

v2 회귀 — `holding_days`: v2-native LIVE SELL 35건 전건 NULL (execution 재쿼리 교차확인: filled 0 / null 35), v1 은 평균 2.94일 기록. **단순 로깅 누락이 아니라 기능 결함이다** — `Position.bought_at` 미설정 때문에 holding_days 가 계산 안 되고, 이는 Time Exit·Fixed Stop 의 시간조임(time-tightening) 로직을 동작 불능으로 만든다(코드 근거·심층분석은 execution 산출물 `2026-05-22-v2-teardown-execution.md §3.1`, `monitor/app.py:238-241`). 청산사유 자체는 v2 도 `reason` enum 으로 정상 기록 — 회귀는 `holding_days`/`bought_at` 계열에 국한.

---

## 부록 — 실데이터 핵심 수치

- v2 DB: `jennie_db` 111 테이블 / 197M / 운영 2026-02-19~04-17.
- daily_asset_snapshots(65행, v1 25 + v2 ~40): total_asset Feb 19 204.2M → 저점 Mar 31 166.0M(-18.7%) → Apr 17 200.6M(-1.8%). `total_profit_loss`(미실현)는 03-03 이후 retire 까지 내내 음수(-11M~-43M), `realized_profit_loss` 는 03-03 이후 전부 0. 포지션 4종이 03-04~04-14 동결.
  - 주의: 위 -18% 드로다운의 대부분은 **emergency_stop 중 동결 포지션의 평가손**이지 v2의 능동적 매매 손실이 아님. 능동 매매 손익 해석은 execution 영역.
- dag_run: 6,910 / 최종실패 0. task_instance: 7,097 success / 0 fail / 평균 40초.
- scout 산출(daily_quant_scores, is_active): 일 ~130종목 스코어링 → final_selected 25 (= `SCOUT_MAX_WATCHLIST_SIZE`, compose). 4월 동안 final 8→25 로 워치리스트 재성장.
- signal_logs: 408,941행 전량 suppressed/emergency_stop (03-06~04-17, ~13,600/일).
</content>
</invoke>
