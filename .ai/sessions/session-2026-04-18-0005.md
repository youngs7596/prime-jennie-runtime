# 세션 핸드오프 — 2026-04-18 (real mode 전환 + backtest persistence + Scout 실전 배선)

**세션 범위**: 백테스트용 재현성 persistence → Scout 실전 모드 전부 배선 → real 모드 전환
**시작**: 2026-04-18 오후 (복구 세션 종료 직후) · **마감**: 같은 날 저녁
**팀**: lead (Opus 4.7 1M) + teammate 6명 (bypass / feeders / adapter / backfill / llmstats / candidates-ui)
**운영 영향**: v2 잔존 12 컨테이너 완전 퇴역 + paper → real 전환 + stop 이중 차단

## 1. 백테스트 Persistence 레이어 (migration 012)

사용자 질문 "백테스트를 위해서 뭘 저장하는거야?" 로 시작. v2 는 "종목 리스트 25 개" 만 남기면 됐지만 v3 는 Python 코드를 뱉어 실행하는 구조라 재현에 필요한 것이 달라짐.

### migration 012 (`migrations/012_backtest_persistence.sql`)
- `scout_runs.context_snapshot_json` JSONB 컬럼 — 실행 시점의 `news_scores` / `sector_momentum` / `universe_hash` / `universe_size` / `macro_size_multiplier` / `macro_gate` / `macro_run_id` / `as_of` / `trigger_reason` 스냅샷.
- `screening_candidates` 테이블 신설: PK=(scout_run_id, rank). 컬럼: ticker / strategy_tag / conviction / entry_hint_json / exit_hint_json / factors_json / notes / **promoted_to_sheet_id** (승격 시) / **rejection_reason** (탈락 시 macro_closed/deprecated_tag/unknown_tag/no_policy/duplicate_today/size_below_min/validator_hallucination/engine_error/publisher_error). 3개 인덱스.
- `position_sheets.provenance_json->>'macro_run_id'` partial 인덱스.

### 코드
- `ProvenanceSection.macro_run_id` top-level 필드 (gate_run_id 와 중복이지만 SQL 조인 편의).
- `StrategyEngine.build_sheet_with_reason(candidate, inputs) -> (sheet | None, reason | None)` — 거부 사유 6종 코드화. 기존 `build_sheet` 는 (sheet 만) 유지해 테스트 비파괴.
- `persist_scout_run` 에 `context_snapshot` 인자 추가.
- `persist_screening_candidates` / `update_candidate_promotion` 신설 — raw 후보 일괄 INSERT + engine 결정 시 update.
- `slow_loop/pipeline.py` 가 screening 직후 candidates 기록 + validation hallucination / engine 결정 / publisher 실패 시 각각 적절한 rejection_reason 으로 UPDATE.

커밋: `5676496 feat(persistence): 백테스트 재현성 축적 — context snapshot + raw candidates + rejection reason`

## 2. Scout 실전 모드 배선 (Agent Team — 4 트랙 병렬)

첫 smoke 에서 "LLM 은 진짜 돌지만 input/executor 둘 다 stub" 임이 드러남. 설계 원칙은 남아있되 Phase 2.9 slice3 미완 상태. 4 트랙 병렬로 해소.

| 트랙 (teammate) | 파일 | 커밋 |
|---|---|---|
| **bypass** | `macro/post_processor.py` + env `MACRO_AUTO_OVERRIDE_DISABLED` | `64d0d6d` |
| **backfill** | `scripts/backfill_daily_prices.py` (KIS 60일 universe 적재) | `c2d9422` |
| **feeders** | `slow_loop/scout/feeders/real.py` (Universe/News/Sector/Market) + `app.py` 배선 | `470a4e5` merge |
| **adapter** | `app.py` ScreeningToolAdapter wire + `pipeline.py` market_data_records + `executor.py` DataFrame round-trip + `scout/market_data_loader.py` | `06ba2e7` merge |

### Scout 프롬프트 교정 (실측 기반)
첫 실전 run 에서 Scout LLM 이 market_data MultiIndex 를 단일 index 로 가정해 filter 전량 탈락 (0 candidates). prompt 에 실 구조 예시 + `.groupby(level='ticker')` / `.xs()` / `.unstack()` 패턴 + 안전 템플릿 + 안티패턴 3종 추가.
- `2f742c7 feat(scout): system prompt 에 market_data MultiIndex 구조 + 안전 템플릿`
- 교정 후 같은 data + context 로 **candidates 10건 생성** 실측 (`sr_smoke_d4f1c97c`).

### 의존성 / allowlist 정리
- slow-loop Dockerfile: `[slow_loop,screening]` extras 필수 (pandas). `39e9e7f`
- `import scipy` 허용: `efd487f`
- `talib` 제거 (컨테이너 미설치): `0cc266e`
- `sklearn` 제거 (컨테이너 미설치): `37aa4b4`

### daily_prices backfill 실측
200 종목 × 60일 요청 → KIS 가 ~23 영업일 반환 → **4590 rows / 153 tickers** 적재 완료. 47 종목은 market_cap NULL / 상장폐지 등으로 fallout.

## 3. 운영 UI 보강 (2 트랙 병렬)

### LLM stats 이력 업데이트 안 됨 (bug)
`dashboard/routers/llm_stats.py` 가 Redis `llm:stats:{date}:{service}` HGETALL 을 읽는데 **쓰는 코드 자체가 없었음**. scout/macro LLM 호출 후 HINCRBY 가 빠져있어 UI 상단 호출 이력 항상 빈 값.

- **수정 (llmstats teammate)**: `infra/llm_stats.py` 신규 `record_llm_call(redis_client, service, input_tokens, output_tokens, cost_usd)` 헬퍼. `persist_scout_run` / `persist_macro_run` 끝에 호출. macro 는 shadow_result 있으면 `service="macro_shadow"` 도 병행.
- **frontend**: `useLLMStats` / `useLLMMonthlyStats` / `useScoutLatest` / `useScoutRuns` 에 `refetchInterval` 추가.
- runtime `51c38df` + control-ui `e462df1`.
- smoke 1회 후 Redis 에 3 키 실측 확인: `llm:stats:2026-04-18:scout` (calls=1, tokens_in=2729, tokens_out=2060, cost=0.003) / `:macro` / `:macro_shadow`.

### Scout candidates UI (feature)
Scout 페이지에서 screening_candidates 를 볼 수 있게.

- **backend**: `GET /scout/runs/{id}/candidates` 엔드포인트. rank 순. `ScreeningCandidateRow` 모델.
- **frontend**: Scout.tsx Run Detail 아래 "Candidates (N)" 테이블 (rank / ticker / strategy / conviction bar / status / notes).
- status: promoted → green + sheet_id prefix / rejected → red + rejection_reason / pending → gray italic.
- runtime `a6d6c44` merge + control-ui `a1308d7`.

### 종목명 추가 (follow-up 요청)
사용자 "종목이름도 보여야 할거 같아" — `stock_masters` LEFT JOIN 으로 `stock_name` 필드 추가. ticker 셀 2줄 (코드 + 이름). runtime `10d6d7e` + control-ui `d03afb4`.

### Logs 뒤죽박죽 (bug)
Loki 가 label set 별로 여러 stream 을 돌려주는데 (kis-gateway 는 stdout/stderr 분리), `dashboard/routers/logs.py` 가 스트림 순차 concat 만 해서 시각 순서가 섞임. ERROR 블록 전체 + INFO 블록 전체 각자 내림차순. timestamp 기준 전역 desc 정렬 + limit 을 병합 후에 적용. `03a7cd8`.

## 4. v2 잔존 12 컨테이너 완전 퇴역

Phase 2.10 에서 22→17 로 줄였다고 했지만 실제로는 `prime-jennie-*` (non-runtime) 중 14 개가 여전히 실행 중이었음. 이번 세션에서 `control-ui` 가 포트 80 충돌로 recreate 실패 → 추적 결과 v2 `prime-jennie-dashboard-frontend-1` 가 port 80 선점. 이를 포함해 **12 컨테이너 제거**:

- price-monitor / buy-scanner / scout-job / sell-executor / buy-executor
- airflow-scheduler / airflow-webserver
- kis-gateway / dashboard / dashboard-frontend
- news-pipeline / telegram / job-worker

**남은 v2 3개 (v3 와 공유, 설계대로)**: vllm-llm / vllm-embed / qdrant.

제거 과정: `docker update --restart=no` → `docker stop` → `docker rm`. 이미지 (`ghcr.io/youngs7596/prime-jennie:latest`) 는 롤백용으로 남김.

## 5. Paper → Real 모드 전환 (세션 말미)

사용자 "mock 모드의 한계구나. stop 걸고 real 로 전환하자" — KIS paper 2/sec rate limit (EGW00201 throttle) 이 monitor balance polling 과 충돌하던 증상을 계기로 결정.

### 실행 순서 (stop 먼저)
1. Redis `trading_flags:stop=1` + `control.state:stop` SET
2. `.env` 5줄 교체 — APP_KEY / APP_SECRET / ACCOUNT_NO / BASE_URL / IS_PAPER (paper → real, 계좌 50156036 → 68211289, openapivts:29443 → openapi:9443). 백업 `.env.bak.paper.20260418_0929`.
3. Paper token 백업 + 제거 (재인증 강제).
4. kis-gateway 재기동 → real OAuth 토큰 신규 발급.
5. `/api/balance` smoke — **실계좌 응답 정상**: 3 포지션 (현대차 266주 -1.06%, 고려아연 15주 -17.52%, HD현대 123주 -12.40%), 현금 341,498원, 총자산 200,552,998원.

### 이중 차단 검증 (손절 방지)
1. **Redis**: `trading_flags:stop=1` 정상. fast-loop `BalanceAwareSizer.__call__` 에서 `entry_allowed=False` → `qty=0` → entry skip.
2. **position_sheets DB**: v2 에서 산 3 포지션에 대한 v3 시트 **0 rows**. fast-loop `sheet_fetcher()` 가 빈 리스트 반환 → tick 들어와도 exit rule 평가 대상 없음.
3. **Stream ACK-first** (`redis_streams.py:157`): consumer 가 XACK 먼저 → pending 에 남지 않음. stop 해제 시 "우다다다" 일괄 폭주 불가.
4. **PositionSheet.valid_until** Pydantic validator: 장외 생성 시트는 자체 거부.
5. **Strategy Engine.duplicate_today**: 같은 날 같은 ticker 중복 시트 차단.

### MACRO_AUTO_OVERRIDE_DISABLED 영구 주입
real 전환 시 KOSPI 20d vol 58% 상태에서 auto_override 가 계속 closed → Scout 가 못 돌아 데이터 축적 안 됨. stop 이중 차단된 상태에선 데이터 축적이 우선이라 `docker-compose.yml` slow-loop env 에 영구 추가 (default 1). **실 매매 재개 전 반드시 제거 또는 "0" 으로 덮어쓸 것**.

## 6. 결정 이력

1. **백테스트 재현 레이어 = v2 candidates 리스트가 아니라 context_snapshot + screening_candidates + code_text**
   - 이유: v3 는 Python 코드 생성 모델이라 "무엇을 골랐나" 뿐 아니라 "어떤 입력을 봤고 어떻게 변환했나" 가 재현 단위
   - 영향: 월요일부터 하루 7 run × ~20 candidates 가 자동 축적. 2~3 주 후 backtest 엔진 슬라이스에 곧바로 사용 가능

2. **Scout 실전화 = 4 트랙 worktree 병렬** (Agent Team)
   - 이유: 블로커가 직교 (bypass/feeders/adapter/backfill) 라 병렬화 이득 명확
   - worktree isolation 실제로는 워크디렉토리 공유였음 (lead + teammate 전원 동일 git repo). 파일 분리 운 + 각자 branch push 로 무사 — 향후 isolation 재점검 필요

3. **MACRO_AUTO_OVERRIDE_DISABLED 영구 주입** (2026-04-18)
   - 이유: real 전환 + stop 차단 기간엔 데이터 축적 우선. Macro LLM 의 원본 판단은 여전히 metadata 에 저장되어 감사 가능
   - 위험: stop 해제 시 이 env 도 함께 제거해야. 안 하면 실 고변동성 구간에 매수 신호가 발행되고 주문까지 간다. **체크리스트 필수 항목**

4. **v2 12 컨테이너 완전 퇴역 + vllm/qdrant 3 만 잔존**
   - 이유: port 80 / 8080 phantom binding 의 근본 제거. 운영 노이즈 최소화
   - 영향: v2 compose `docker compose up` 실수 시에도 3개만 부활. prime-jennie repo (youngs7596) 쪽은 compose 파일은 그대로 — 필요 시 편집

## 7. 월요일 장 시작 후 관측 포인트

- 09:30 KST 첫 scout_daily tick → `scout_runs` 1 row + `screening_candidates` ~20 rows + `position_sheets` N rows (stop 로 체결 없이 발행만)
- 10:30 / 11:30 / 12:30 / 13:30 / 14:30 — 동일 패턴 6회 추가. 총 7 run / 일
- UI Scout 페이지: 좌측 목록에 7 run + 우측 Run Detail (code + context) + 하단 Candidates (종목명 포함) 실시간 폴링
- UI Overview LLM stats: 일일 누적 calls / tokens_in / tokens_out 증가 실측
- **반드시 `trading_flags:stop=1` 유지 확인** (자동 해제 경로 없으므로 사용자가 명시적으로 `SET 0` 해야만 풀림)
- Scout 프롬프트 교정 효과 관찰 — candidates_count 실측 분포 (목표: 평균 5~15 건/run)

## 8. Phase 2.15 후보

- Scout 프롬프트 few-shot 예제 추가 (현 안전 템플릿은 스켈레톤, 실 성공 코드 1~2 개 삽입 시 품질 상승 기대)
- `valid_until <= 15:30 KST` validator 가 장외 smoke/테스트 경로를 항상 차단 — `SCOUT_VALID_UNTIL_BYPASS` env 또는 debug 전용 우회 로직
- `rejection_reason="engine_error"` 분기 세분화 (현재는 Exception 통째로 engine_error 로 잡힘 — `ValidationError`/`valid_until` 은 `after_hours` 같이 명시)
- KIS paper → real 전환 체크리스트를 `docs/RUNBOOK_REAL_MODE.md` 로 별도 문서화
- `MACRO_AUTO_OVERRIDE_DISABLED` 제거 시 자동 알림 (dashboard 에 경고 배지)
- 매수 신호 발행 시 Telegram 알림 (stop 과 무관하게 정보성)
- worktree isolation 이 실제로 분리된 디렉토리로 동작하는지 harness 차원 검증

## 9. 커밋 체인 요약

```
0cc266e  fix(screening): talib 제거 (컨테이너 미설치, scipy 대체)
37aa4b4  fix(screening): sklearn 제거 (컨테이너 미설치)
efd487f  fix(screening): scipy 전체 허용
39e9e7f  chore(docker): slow_loop image 에 screening extras
06ba2e7  merge: adapter — ScreeningToolAdapter wire + market_data DF
4f4b39b  feat(scout,adapter): Real ScreeningToolAdapter 배선
470a4e5  merge: feeders — Real Scout feeder 4종
846f58c  feat(scout): Real feeder 4종 (universe/news/sector/market)
c2d9422  feat(scripts): daily_prices one-shot backfill
64d0d6d  feat(macro): MACRO_AUTO_OVERRIDE_DISABLED env bypass
5676496  feat(persistence): 백테스트 재현성 축적
2f742c7  feat(scout): prompt MultiIndex 구조 + 안전 템플릿
a6d6c44  merge: candidates-ui — /scout/runs/{id}/candidates + UI
a95d1ab  feat(scout): GET /scout/runs/{id}/candidates
51c38df  merge: llmstats-write — record_llm_call + polling
b46fc2b  feat(llm-stats): slow_loop scout/macro Redis 누적 쓰기
10d6d7e  feat(scout): candidates 응답에 stock_name 추가 (stock_masters LEFT JOIN)
03a7cd8  fix(logs): Loki 다중 스트림 병합 시 timestamp desc 전역 정렬
```

control-ui (youngs7596): `a1308d7` (candidates UI) + `d03afb4` (stock_name) + `e462df1` (polling).

## 10. Rollback 참고

### Real → Paper 복귀
```bash
cd ~/projects/prime-jennie-runtime
cp .env.bak.paper.20260418_0929 .env
rm data/kis_token/v3_kis_token.json
mv data/kis_token/v3_kis_token.json.paper.20260418_0929 data/kis_token/v3_kis_token.json
docker compose up -d --force-recreate kis-gateway
```

### MACRO_AUTO_OVERRIDE 재활성
`docker-compose.yml` slow-loop environment 에서 `MACRO_AUTO_OVERRIDE_DISABLED: ${…:-1}` 줄 제거 또는 `.env` 에 `MACRO_AUTO_OVERRIDE_DISABLED=0` 설정 후 recreate.

### Stop 해제 (매매 재개)
```bash
docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a <PW> SET trading_flags:stop 0
docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a <PW> DEL control.state:stop
```
**단, MACRO_AUTO_OVERRIDE_DISABLED 를 먼저 제거해야 실 고변동성 보호가 복구됨.**
