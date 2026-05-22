# v3 Assessment — 오케스트레이션 도메인

작성: orchestration 에이전트 / 2026-05-22 / v2-teardown 팀 Step 2
대상: v3 = prime-jennie-runtime repo + MS-01 라이브 (~2026-04-17 가동, 조사 시점 ~5주 운영)
입력 체크리스트: `2026-05-22-v2-teardown-orchestration.md §4` v3 비교 훅 8항
원칙: 코드·데이터가 진실, 양방향 냉정, 패딩 금지, 읽기 전용. v2-native 성과는 미검증 베이스라인 — "v3가 v2 성과보다" 식 판정 안 함. 비교축 = (a)설계 (b)v3 라이브 데이터.

## 1. 훅별 판정표

| # | 훅 | 판정 | 근거 (요약) |
|---|---|---|---|
| 1 | 스케줄링 책임 분리 | **KEPT (재구현)** | Airflow → apscheduler + `scheduled_jobs` 테이블. 스케줄(테이블)/로직(handler) 분리 유지. control-ui 가 SQL CRUD. |
| 2 | 운영 안정성 기준선 | **부분 LOST** | scheduled_job_runs 6,929건 / failed 13 (0.19%). 단 **스케줄러 레벨 retry 제거** — resilience 가 handler 별 분산·불균일. |
| 3 | "성공" 검증 깊이 | **KEPT (동일 약점)** | v2 HTTP 200 = v3 "예외 없으면 success". STOP 스킵 run 도 success 기록. duration_ms 로 사후 식별만 가능. |
| 4 | 재실행/이력 모델 | **IMPROVED** | run_id+is_active 모델 유지(daily_quant_scores·watchlist_histories) + scout_run→screening_candidates→sheet FK lineage 확장. |
| 5 | 실시간 매매 파이프라인 | **KEPT + NEW** | Redis Streams 유지(v2 발명). 4서비스(scanner/buyer/seller/monitor)→`fast-loop` 1개 통합. `coordinator-listener` event archive 신규. |
| 6 | Kill-switch | **IMPROVED 메커니즘 / NEW-DEFECT 범위** | 명령 stream + Pydantic ControlCommand + 4-state(stop/pause/dryrun/liquidate). 단 slow-loop STOP 범위 과잉(→ #8). |
| 7 | 데이터 모델 위생 | **IMPROVED** | Postgres 31 테이블, 전부 v3-native·단일 명명, Airflow 메타 없음. `016_drop_legacy_tables.sql` 로 레거시 명시 정리. |
| 8 | STOP 함정 | **NEW-DEFECT (대표)** | v3 STOP = 분석 루프 hard-kill. 05-18~20 macro_runs 0~2 / scout_runs 0 / event_log entry_decided 0. |

---

## 2. 회귀 (Regressions)

### 2.1 [대표] STOP 이 관측 모드 → 암흑 모드 (훅 #8)

v2 STOP(emergency_stop)은 **거래만** 멈추고 분석·관측은 계속 — scout/macro DAG 가 retire 까지 돌았고 signal_logs 에 408,941건 suppressed 신호가 남았다(백테스트 자산).

v3 STOP 은 **분석 루프 자체를 죽인다.** `slow_loop/pipeline.py:322-336` — `run_slow_loop()` 최상단에서 `SystemState.stopped` 면 즉시 `SlowLoopResult(skipped_reason="control_stopped")` 로 early-return. 주석(:318-321) 의도는 "STOP 후 시트가 쌓이는 혼란 방지"지만, 게이트가 Macro phase·Scout phase **앞**에 있어 LLM 분석 호출 + 시트 발행이 **전부** skip 된다.

라이브 데이터 (2026-05-18~20 STOP 구간):

| 날짜 | macro_runs | scout_runs | event_log entry_decided | scout_daily job duration |
|---|---|---|---|---|
| 05-15 (정상) | 50 | 5 | 616 | 6–55초 |
| 05-18 (STOP) | 2 | 0 | 0 | **2–5ms (skip)** |
| 05-19 (STOP) | 0 | 0 | 0 | **2–49ms (skip)** |
| 05-20 (STOP) | 0 | 0 | 0 | **4–36ms (skip)** |
| 05-22 (정상) | 1+ | 1+ | 3,104 | 25초 |

slow-loop 뿐 아니라 fast-loop 도 STOP 중 `entry_decided` 이벤트 0 — 양쪽 루프가 동시에 암흑. v3 엔 v2 signal_logs 같은 suppressed-signal 적재처가 없어, STOP 구간엔 매크로·스카우트·진입판단 **어떤 분석 데이터도 남지 않는다.** 이것이 thesis gate 측정 윈도우(5-18~5-21) 표본 0 의 직접 원인 (MEMORY `thesis_gate_deferred`).

### 2.2 스케줄러 레벨 retry 상실 (훅 #2)

v2 Airflow 는 모든 DAG 에 `retries=1~2` 를 선언적으로 부여 — transient 실패 52건이 전부 재시도로 흡수돼 최종실패 0. v3 `infra/scheduler.py` `SchedulerRunner.run_job`(:395-427)은 handler 예외 시 `status="failed"` 기록 후 **그대로 return — 재시도 없음.** apscheduler `add_job` 에도 retry 설정 없음(:384-393).

결과: resilience 가 handler 마다 제각각. scout handler 는 내장 3-attempt(`scout failed after 3 attempts`), macro 는 `_run_macro_with_retry`. 반면 `daily_asset_snapshot` 은 재시도 0 — 2026-04-20~30 `ConnectError` 로 **9 거래일 연속 hard-fail**(asset snapshot 9일 공백). 13개 실패 run 중 11개가 이 단일 job.

(주: 9일 실패의 root cause 는 `KIS_GATEWAY_URL` env 누락 — 결정론적 버그라 retry 로는 못 막음(MEMORY `feedback_asset_snapshot_retry`). 하지만 retry 부재는 05-11/13 의 KIS 503 같은 진짜 transient 실패까지 hard-fail 로 만든다.)

### 2.3 STOP 스킵 run 이 "success" 로 집계 (훅 #3)

v2 약점("HTTP 200 = success")이 형태만 바뀌어 잔존. v3 SchedulerRunner 는 handler 가 예외 없이 끝나면 success. STOP 중 `scout_daily` 가 `skipped_reason` 을 담아 정상 return → `scheduled_job_runs.status='success'`. 05-18~20 의 스킵 21회가 success 6,916건에 포함. 식별 단서는 `duration_ms`(2ms vs 25,000ms)뿐 — handler 가 skip 결과를 반환해도 status 에 반영 안 됨.

---

## 3. 진짜 개선 (Real Improvements)

### 3.1 데이터 모델 위생 (훅 #7) — 명백한 개선

v2 `jennie_db` = 111 테이블(앱 + Airflow 메타 + v1 레거시 단·복수 중복 혼재). v3 `prime_jennie_v3` = **31 테이블, 전부 v3-native, 단일 복수형 명명, Airflow 메타테이블 없음.** `migrations/016_drop_legacy_tables.sql` 로 레거시를 명시적으로 drop. MEMORY 에 죽은 테이블로 기록된 `daily_macro_insights` 도 실제 부재 확인. 백업·용량·쿼리 모호성이 구조적으로 줄었다.

### 3.2 run_id 이력 + 명시적 lineage (훅 #4) — 유지 + 확장

v2 의 `run_id`+`is_active`(migration 008) canonical-run 모델을 v3 `daily_quant_scores`·`watchlist_histories` 가 그대로 계승(컬럼 확인). 추가로 `scout_runs(scout_run_id)` → `screening_candidates(scout_run_id FK, promoted_to_sheet_id, rejection_reason)` → `position_sheets` 로 **실행 단위 lineage 가 FK 로 명시**. v2 가 하루 7회 실행을 run_id 로 보존한 설계를, v3 는 실행→후보→시트 추적까지 연장.

### 3.3 scheduled_jobs 운영성 (훅 #1)

v2 Airflow 는 DAG 변경 = 파일 편집 + 재파싱. v3 `scheduled_jobs` 는 평범한 Postgres 테이블이라 control-ui 가 SQL 로 enable/cron/kwargs 를 즉시 CRUD, Redis pub/sub `scheduler.reload:{owner}` 로 즉시 반영(`infra/scheduler.py:344-364`). `scheduled_job_runs` 가 run_id·started/finished·status·error·duration_ms 를 보존 — v2 Airflow `dag_run`/`task_instance` 와 동급 관측성을 가벼운 테이블 2개로 재현. cron dow 변환 버그(apscheduler 0=Mon vs cron 0=Sun)를 `_normalize_cron_for_apscheduler`(:90-97)로 정조준 수정 — 실제 함정을 잡은 코드.

### 3.4 coordinator-listener — 견고한 신규 스트림 인프라 (훅 #5)

v2 엔 없던 컴포넌트. `coordinator:events` Redis Stream 을 consumer group 으로 구독해 `event_log` 에 archive(`coordinator/listener.py`). 품질 포인트: at-least-once + `stream_msg_id` UNIQUE → `ON CONFLICT DO NOTHING` idempotency; **DLQ**(delivery_count≥5 격리) 로 깨진 메시지가 backlog 를 막지 않음 — v2 의 `feedback_legacy_consumer_groups`(stuck group 이 XLEN 폭주) 류 사고를 구조적으로 차단; Pydantic discriminated-union 검증. 라이브: event_log 3,902건(05-14~22). v2 stream 운영보다 분명히 견고하다.

단 — Coordinator 는 아직 "Stage 1 read-only archive + Stage 2 advisory" 단계. `decision_log` 라이브 2건, `outcomes` 0건. 즉 현재의 Coordinator 는 **이벤트 기록기**이지 아직 "조율자"는 아니다 (설계상 의도된 단계 — 과대평가 금물).

---

## 4. 새 결함 (통합 이음매 위주)

### 4.1 [높음] STOP 게이트 위치 — 분석/관측까지 동반 사망

§2.1. 결함의 본질은 "게이트 위치": STOP 차단 의도는 *시트 발행*인데 게이트가 `run_slow_loop` 최상단이라 Macro·Scout *분석*까지 끌고 죽는다. 이음매 = control.state ↔ slow_loop pipeline.

### 4.2 [중간] 통합 이음매 env 누락 — daily_asset_snapshot 9일 공백

job-worker 컨테이너에 `KIS_GATEWAY_URL` 이 안 꽂혀 cfg default(localhost)로 떨어짐 → `ConnectError` 9일. 코드/`.env`/`docker-compose.yml` 삼면 정합 실패의 전형(MEMORY `feedback_code_env_alignment`). 기동 시 필수 env fail-fast 검증이 없어 런타임까지 안 드러남.

### 4.3 [낮음] 스케줄러 ↔ handler 이음매 — skip 가시성 없음

handler 가 `skipped_reason` 을 반환해도 SchedulerRunner 는 success 로만 기록(§2.3). `scheduled_job_runs` 에 skip 상태가 1급으로 없다.

### 4.4 [정보] 미사용 산출물 — `outcomes` 테이블 0행

v2 의 "코드엔 있으나 안 도는 DAG"(collect_us_market) 패턴의 v3 판: 테이블은 있으나 0행. `outcomes` 는 Phase 0 #1(conviction-outcome 상관) use case 의 산출처로 보이나 아직 미연결. (정정: 이건 데이터/selection 영역과 겹치니 교차 확인 필요.)

### 4.5 [정보] 비활성·중복 job 잔존

`scheduled_jobs` 에 enabled=false 가 3건(`collect_full_market_data`, `price_scheduler.collect_minute`, `sync_positions`). `collect_minute` 는 `job_worker.collect_minute_chart` 와 기능 중복으로 한쪽만 살아있음 — 소소한 정리 미스. (`sync_positions` off 는 MEMORY 상 의도된 것 — 정상.)

---

## 5. Step 3 보완 후보 (우선순위·규모 — elaborate 설계 아님)

| 순위 | 항목 | 규모 | 메모 |
|---|---|---|---|
| **1 (높음)** | STOP 게이트를 publish 직전으로 이동 | 작음 (~30줄) | `run_slow_loop` 에서 STOP 시 Macro·Scout 분석은 실행해 macro_runs/scout_runs 적재, **시트 발행만** skip. thesis-gate 표본 0 의 직접 해소. v2 "관측 모드" 복원. |
| 2 (중간) | scheduler-level retry | 중간 | `scheduled_jobs` 에 `max_retries` 컬럼 + `run_job` 에 재시도 루프. transient(503 등) 흡수. 결정론 버그는 여전히 retry 무효 — fail-fast(아래 3)와 병행. |
| 3 (중간) | 기동 시 필수 env fail-fast | 작음 | job-worker 등 KIS 의존 서비스가 `KIS_GATEWAY_URL` 미설정이면 기동 거부. §4.2 류 9일 공백 차단. |
| 4 (낮음) | `scheduled_job_runs` 에 `skipped` status | 작음 | handler 가 skip 결과 반환 시 success 대신 skipped. §2.3/§4.3 가시성. |
| 5 (정보) | `outcomes` 연결 또는 정리 | — | selection/data 영역과 교차. 미사용 테이블 방치 vs 파이프라인 연결 결정 필요. |
| 6 (정보) | 비활성 중복 job 정리 | 사소 | `price_scheduler.collect_minute` 등 제거. |

핵심은 **#1** — v3 오케스트레이션의 단일 대표 결함이고, 규모가 작은데(게이트 위치 한 곳) 효과가 크다(STOP 중 관측 데이터 복원 → thesis gate 진행 가능).

---

## 부록 — 라이브 수치

- v3 컨테이너 ~20개 (v2 ~20개와 동급 — Airflow 2개 제거, coordinator-listener 추가, scanner/buyer/seller/monitor 4→fast-loop 1 통합).
- `scheduled_jobs` 29건 (job_worker 26 / price_scheduler 2 / slow_loop 1). v2 Airflow 22 DAG 대비 확장(news·reconcile·macro 분할).
- `scheduled_job_runs` 6,929건 (success 6,916 / failed 13), 2026-04-17~05-22.
- 실패 13건: daily_asset_snapshot 11 (ConnectError 9 + 503 2), scout_daily 2 (LLM parsing_error).
- `event_log` 3,902건 (05-14~22): entry_decided 3,720 / sheet_published 50 / 기타. `decision_log` 2건, `outcomes` 0건.
- `position_sheets` 1,355건(04-18~), `executions` 278건.
- STOP 구간 05-18~20: macro_runs 0~2/일, scout_runs 0/일, event_log entry_decided 0/일.
</content>
</invoke>
