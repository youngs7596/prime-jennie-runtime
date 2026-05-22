# Prime Jennie Runtime — 자가진화 KOSPI/KOSDAQ AI 트레이딩 엔진 (v3)

<div align="center">

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Python](https://img.shields.io/badge/python-3.12+-green)
![Docker](https://img.shields.io/badge/docker-compose-2496ED)
![Tests](https://img.shields.io/badge/tests-1405%20passed-brightgreen)
![Selection](https://img.shields.io/badge/selection-deterministic%20quant-green)
![Mode](https://img.shields.io/badge/runtime-real-red)

**KOSPI 자율 트레이딩 엔진 — v3 실행 런타임**

*"결정론이 종목을 고르고, 빠른 루프가 집행한다."* (2026-05-22 LLM 코드 생성 폐기)

</div>

---

## 목차

- [개요](#개요)
- [빠른 시작](#빠른-시작)
- [운영 모드 (Paper / Real)](#운영-모드-paper--real)
- [핵심 기능](#핵심-기능)
- [시스템 아키텍처](#시스템-아키텍처)
- [서비스 구성](#서비스-구성)
- [기술 스택](#기술-스택)
- [프로젝트 구조](#프로젝트-구조)
- [데이터 흐름](#데이터-흐름)
- [Exit Rules 체계](#exit-rules-체계)
- [리스크 관리](#리스크-관리)
- [설정](#설정)
- [테스트](#테스트)
- [모니터링](#모니터링)
- [배포 파이프라인](#배포-파이프라인)
- [관련 리포](#관련-리포)

---

## 개요

**Prime Jennie Runtime**(v3)은 한국투자증권 Open API 기반 KOSPI/KOSDAQ AI 자율 트레이딩 엔진의 **실행 런타임**입니다. v2(`prime-jennie`)의 단일 거대 모놀리식 구조를 분해하여 **느린 루프(Slow Loop) + 빠른 루프(Fast Loop) + 메타 진화(Meta)** 의 3-레이어로 재구성한 후속 세대입니다.

### Scout 선정 아키텍처 — 두 차례의 결정

v3 Scout 선정은 두 번의 설계 전환을 거쳤습니다.

**1차 (v3 초기, 2026-04~05-21) — LLM 코드 생성**
LLM 에게 시장 데이터를 주고 그 데이터를 스크리닝할 Python 코드를 생성하게 했습니다. 격리 Docker 샌드박스에서 결정론적으로 실행 → 재현성·검증성을 자가진화의 토대로 삼겠다는 의도. 검증 닫힌 루프(2026-04-25)까지 도입했습니다.

**2차 (현재, 2026-05-22~) — 결정론 quant 코어 복원**
LLM 코드 생성을 폐기하고, v2 에서 안정적이었던 결정론 quant 스코어러로 복원했습니다. 결정 배경:

- 2026-05-15 — 같은 거래일에 -5% 손절한 종목을 다시 매수해 또 손절하는 비정상 동작 발견. cooldown 가드 등 보강을 했으나 신뢰 회복 안 됨.
- v3 본격 가동(2026-05-06) 이후 실현 순손실 약 **-1,377만원** (132건 청산: 손절 89건 -1,867만 vs 추적익절 29건 +452만). 손절이 익절의 3배 빈도. 승률 27%.
- 결정: 비결정성을 코드 생성 단계에서마저 빼고 v2 의 검증된 결정론 스코어러로 복원.

**결정론 코어 (`run_deterministic_scout`)**
7개 팩터(모멘텀·품질·가치·기술·뉴스·수급·섹터모멘텀)를 코드 상수로 정의된 공식으로 가중 합산해 100점 만점. 직전 2회 런까지 3회 평균 점수로 평활하고, 신규 진입 ≥ 62점 · 이탈 < 55점 히스테리시스로 잦은 전환을 막습니다. 선정 경로 LLM 호출 0회. v2 `quant.py` 포팅.

**Macro Gate** 는 여전히 LLM(DeepSeek reasoning + Claude Opus shadow) 기반이지만, 그 위에 결정론 안전망(5가지 closed 조건 · 2단계 이산화 · auto_override) 이 이중으로 씌워져 있습니다. "LLM-at-core 폐기" 는 종목 선정(Scout) 에만 해당하며, Macro 의 비결정성은 결정론 게이트로 가둬 둔 채 유지합니다.

**아키텍처 변경 이력**

| 시점 | 변경 |
|---|---|
| 2026-04-16 | v3 LLM-at-core 코드 생성 아키텍처 확정 (민지 × 영석) |
| 2026-04-25 | Scout 검증 닫힌 루프 도입 |
| 2026-05-15 | 같은 거래일 재진입 사고 → Awareness/Cooldown 가드 도입 |
| 2026-05-17 | G 시리즈 명명 폐기, thesis_aware_hold Phase A 영속 |
| **2026-05-22** | **LLM-at-core 폐기, v2 결정론 quant 코어 복원** |
| **2026-05-22** | **outcomes 적재 경로 복원 — 운영 청산 결과를 outcomes 테이블에 기록 (백필 132건 -1,377만원)** |

설계 결정 기록: [`.ai/decisions/2026-05-22-selection-architecture-decision.md`](./.ai/decisions/2026-05-22-selection-architecture-decision.md)

원래 4종 설계 문서 중 [`SCOUT_CODE_GENERATION.md`](./docs/SCOUT_CODE_GENERATION.md) 는 1차 아키텍처 시절 명세이므로 현재는 **역사 자료**입니다. 나머지 ([`prime_jennie_v3_phase0_design.md`](./docs/prime_jennie_v3_phase0_design.md), [`POSITION_SHEET_SPEC.md`](./docs/POSITION_SHEET_SPEC.md), [`MACRO_GATE_SPEC.md`](./docs/MACRO_GATE_SPEC.md)) 는 그대로 유효합니다.

### 주요 특징

| 기능 | 설명 |
|------|------|
| **Slow Loop (30분 주기, 평일 08:30~14:30)** | Macro Council(LLM) → Macro Gate(이진) → 결정론 Scout(7팩터 quant) → Strategy Engine → 포지션 시트 발행 |
| **Fast Loop (초 단위, 결정론)** | 포지션 시트 consumer → PendingEntryQueue → BalanceAwareSizer → KIS Gateway → executions / positions / outcomes 기록 |
| **Macro Gate (LLM + 결정론 안전망)** | DeepSeek reasoning + Claude Opus shadow → 결정론 closed 조건 5종 + 2단계 이산화(0.75/1.0) → `gate=open/closed` + `size_multiplier` |
| **결정론 Scout (2026-05-22 복원)** | universe → 7팩터 채점(0~100) → MA(3) 평활 → 히스테리시스(entry≥62 / exit<55) → 최대 20후보. LLM 호출 0회. v2 `quant.py` 포팅 |
| **9-rule Exit System** | `fixed_sl` 필수 + `time_stop` 필수 + 7종 선택. first_match 평가. time_stop·death_cross 배선 2026-05-22 복구 |
| **포지션 시트 (JSON v1.1)** | 발행자/소비자/관찰자가 단일 명세로 합의. provenance 와 context_snapshot 으로 사후 재구성 가능 |
| **outcomes 적재 (2026-05-22 복원)** | 완전 청산 시 record_sell 이 executions 기반 가중평균으로 entry/exit·net pnl·exit_reason 을 outcomes 테이블에 UPSERT. `/pnl`, 대시보드 성과, meta 평가 입력 |
| **News Pipeline** | 한국 뉴스(Naver + Qwen3 메타데이터 추출 → news_events 테이블) + 글로벌 뉴스(WSJ/Bloomberg/Reuters RSS via Google News + DeepSeek digest). Qdrant/임베딩은 폐기 |
| **Control UI (별도 리포)** | React 19 + Vite, FastAPI dashboard 라우터 → stop/pause/dryrun/liquidate 제어 |
| **Telegram 양방향** | 알림 + `/stop` `/pause` `/liquidate` 등 명령. emergency stop 우회 강제 청산 2단계 안전장치 |
| **26 cron job runner** | apscheduler 기반 `scheduled_jobs` 테이블이 단일 진실 (cron 표준 → apscheduler 규약 자동 변환) |
| **Coordinator Listener** | 별도 컨테이너로 매매 이벤트를 event_log 에 아카이브 + advisory 정책(중복/손절 쿨다운) 평가 |
| **관찰성** | Promtail → Loki + Grafana 대시보드 + daemon heartbeat (Redis TTL key) |
| **Backtest 엔진** | `fast_loop.exit_evaluator` 재사용해 운영 경로와 동일 청산 로직. no-lookahead 가드 + bt_ prefix 로 운영 데이터 격리 |

---

## 빠른 시작

### 사전 요구사항

| 필수 | 선택 |
|------|------|
| Docker & Docker Compose v2 | NVIDIA GPU (vLLM 로컬 추론용, 미보유 시 v2 vllm 공유) |
| Python 3.12+ | uv (Python 패키지 매니저) |
| [한국투자증권 Open API](https://apiportal.koreainvestment.com) 발급 | Cloudflare Tunnel (외부 접근) |
| [Telegram Bot](https://core.telegram.org/bots#creating-a-new-bot) 토큰 | Gmail OAuth (WSJ 뉴스레터 수집) |
| DeepSeek + Anthropic API 키 (LLM) | OpenAI API 키 (Cloud 임베딩 fallback) |

### 로컬 개발 환경

```bash
git clone https://github.com/youngs7596/prime-jennie-runtime.git
cd prime-jennie-runtime

# 가상환경 + 의존성
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# minyoung-mah editable 연결 (PyPI publish 전까지)
uv pip install -e ../minyoung-mah

# 인프라만 (개발용 — postgres + redis)
docker compose up -d postgres redis
```

> **주의**: `uv pip install -e` 는 `uv.lock` 을 변경하지 않습니다. CI/배포는 `pyproject.toml` 의 git+tag 핀을 resolve 하므로 로컬과 배포 버전이 달라질 수 있음. 자세한 내용은 [`../minyoung-mah/docs/CONSUMER_SETUP_FAQ.md`](../minyoung-mah/docs/CONSUMER_SETUP_FAQ.md).

### 최초 배포 Run-Book (Bare Metal)

빈 DB 상태에서 처음 올릴 때 순서. 모든 명령은 MS-01 `/home/youngs75/projects/prime-jennie-runtime` 에서 실행.

#### Step 1 — 환경 변수

```bash
cp .env.example .env
vi .env  # KIS 계좌 / Telegram / API 키 / Redis/Postgres 비밀번호 등
```

필수 환경변수 카테고리(비밀값은 이름만):

| 카테고리 | 변수 | 용도 |
|---------|------|------|
| **Infra** | `POSTGRES_PASSWORD`, `POSTGRES_ADMIN_PASSWORD`, `PJ_RUNTIME_PASSWORD`, `REDIS_PASSWORD` | DB/Cache auth |
| **KIS** | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `KIS_BASE_URL`, `KIS_IS_PAPER` | 증권사 API + paper/real 플래그 |
| **LLM** | `VLLM_LLM_URL`, `VLLM_EMBED_URL`, `QDRANT_URL`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` | provider 접근 |
| **Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 + 제어 명령 |
| **관찰성** | `GRAFANA_PASSWORD`, `CLOUDFLARE_TUNNEL_TOKEN` | — |
| **운영 플래그** | `MACRO_AUTO_OVERRIDE_DISABLED` (기본 `1` = bypass 활성) | 고변동성 구간 Macro 자동 폐쇄 비활성화. **실매매 재개 전 반드시 `0` 또는 제거** |

#### Step 2 — DB 초기화 + 마이그레이션

```bash
# postgres 만 먼저 기동 → 001 자동 적용
docker compose up -d postgres redis

# 002~012 순차 적용 (alembic 미사용, psql 직접)
for i in {02..12}; do
  psql -h localhost -U pj_admin -d prime_jennie_v3 \
    -f migrations/0${i}_*.sql
done

# 검증
psql -h localhost -U pj_admin -d prime_jennie_v3 -c "\dt+" | wc -l
# 기대: 17개 테이블
```

마이그레이션 역할:

| # | 내용 |
|---|------|
| **001** | v3 핵심 스키마 (position_sheets, scout_runs, macro_runs, macro_gates, scheduled_jobs) + 4 DB 역할 |
| **002** | v2 legacy_* 테이블 (ETL 임포트용) |
| **003** | 시세 (minute_prices, daily_prices) |
| **004** | 뉴스 (articles, sentiments) |
| **005** | scheduled_jobs + scheduled_job_runs |
| **006** | stock_masters, sector_masters |
| **007** | naver_sectors, us_market_data, investor_trading, foreign_holdings, dart_filings |
| **008** | 매크로 지표 (macro_indicators, consensus_data, roe_analysis, quarterly_financials) |
| **009** | v2 거래 이력 (buy_signals, sell_signals, portfolio_snapshots) |
| **010** | v2 뉴스/거래 보관 테이블 |
| **011** | global_macro_news_articles + digests (Phase 2.13) |
| **012** | screening_candidates + scout_runs.context_snapshot_json (Phase 2.13 백테스트 재현성) |
| **019** | scout_outcomes_v1 view (G1 outcome feedback, 2026-05-15) |

#### Step 3 — Scheduled Jobs Seed

```bash
# 27 cron job 등록 (apscheduler registry)
uv run python -m scripts.seed_scheduled_jobs

# 검증
psql -h localhost -U pj_admin -d prime_jennie_v3 \
  -c "SELECT COUNT(*) FROM scheduled_jobs;"
# 기대: 약 29 rows
```

주요 job 예시:

| job | cron (KST) | 설명 |
|-----|-----------|------|
| `slow_loop.scout_daily` | `30 8-14 * * 1-5` | Scout/Macro 평일 30분 간격 (08:30~14:30) |
| `job_worker.macro_quick` | `*/5 9-15 * * 1-5` | 장중 5분 주기 거시 스냅샷 갱신 |
| `job_worker.collect_minute_chart` | `*/5 9-15 * * 1-5` | KIS 분봉 적재 (top30) |
| `job_worker.collect_full_market_data` | `0 16 * * 1-5` | 일봉 운영 수집 (시총 top300, ETN 제외) |
| `job_worker.reconcile_state_kis` | `*/5 9-15 * * 1-5` | KIS ↔ Redis state 수량 비교 + 알림 |
| `job_worker.daily_briefing_report` | `0 17 * * 1-5` | 17시 일일 브리핑 Telegram 발송 |
| `job_worker.global_news_crawl` | `0 */2 * * *` | WSJ/Bloomberg/Reuters RSS |
| `job_worker.global_news_digest` | `30 7,11 * * 1-5` | Macro pipeline 30분 전 fresh digest |

> 한국 뉴스 파이프라인은 별도 cron 이 아니라 `news-pipeline` 데몬의 인-프로세스 루프(장중 10분 / 장외 30분)로 돕니다 (migration 013 에서 cron 제거).

DB는 **cron 표준** (0/7=Sun, 1=Mon). apscheduler 규약 변환은 런타임에 `infra/scheduler.py._normalize_cron_for_apscheduler` 가 처리.

#### Step 4 — 전체 스택 기동

```bash
COMPOSE_PROJECT_NAME=prime-jennie-runtime docker compose --profile full up -d --build

# 상태 확인 (약 90초 후)
docker compose --profile full ps

# 헬스 체크
curl http://localhost:8090/api/system/health | python3 -m json.tool
```

#### Step 5 — 첫 smoke

```bash
# Balance (paper 모드에서 시작하면 시뮬 계좌)
curl http://localhost:8080/api/balance

# 첫 Scout run 관찰 (다음 cron tick 대기)
docker logs prime-jennie-runtime-slow-loop-1 --since 30m | grep scout_daily

# Grafana 관찰 대시보드
open http://localhost:3300   # admin / $GRAFANA_PASSWORD
```

---

## 운영 모드 (Paper / Real)

| | Paper Mode (기본) | Real Mode (실매매) |
|---|------------------|-------------------|
| **계좌** | 시뮬 계좌 (KIS 모의투자) | 실계좌 |
| **`KIS_IS_PAPER`** | `true` | `false` |
| **`KIS_BASE_URL`** | `https://openapivts.koreainvestment.com:29443` | `https://openapi.koreainvestment.com:9443` |
| **금전 리스크** | 0 | 실제 손실 발생 가능 |
| **데이터 축적** | 시세/뉴스/공시/펀더멘털/수급/지수/미장/AI 로깅 모두 실 시장 데이터로 수집 | 동일 + 실주문 outcome |
| **Macro bypass** | 일반적으로 `MACRO_AUTO_OVERRIDE_DISABLED=1` 유지 | 반드시 `0` 또는 제거 |

**Paper → Real 전환은 반드시** [`docs/REAL_MODE_MIGRATION_CHECKLIST.md`](./docs/REAL_MODE_MIGRATION_CHECKLIST.md) **를 Top-down 으로 실행**. Stop flag 선결 → .env 교체 → 토큰 재발급 → Macro bypass 해제 → Stop 해제 순서 엄수.

### 제어 명령 (Telegram 또는 API)

| 명령 | 동작 |
|------|------|
| `/stop` | 모든 신규 진입 차단 (기존 청산 유지) |
| `/pause <reason>` | 일시 중단 (reason 기록) |
| `/resume` | 재개 |
| `/dryrun` | 시뮬레이션 모드 (주문 전송 대신 로깅) |
| `/liquidate` | 강제 청산 (Add → Arm 2단계 안전장치, emergency stop 우회) |

---

## 핵심 기능

### 1. Slow Loop — Macro(LLM) + Scout(결정론)

```
[Global News Crawler] (WSJ/Bloomberg/Reuters via Google News, 2h 주기)
       ↓
[Korean News Pipeline] (장중 10분 / 장외 30분, Qwen3 메타데이터 추출 → news_events)
       ↓
[Macro Pipeline] (평일 30분 주기)
   - DeepSeek reasoning → MacroGateOutput
   - Claude Opus shadow 병렬 평가
   - 결정론 안전망: closed 조건 5종 + auto_override + 2단계 이산화
       ↓
[Macro Gate] (이진 판정 + 안전망 검증)
   gate: "open" | "closed"
   size_multiplier: 0.0 | 0.75 | 1.0
       ↓
gate=closed → Slow Loop 정지 (Scout 미실행, 다음 cycle 까지)
gate=open  → ↓
       ↓
[Deterministic Scout] (LLM 호출 0회, v2 quant.py 포팅)
   - enrich_universe — daily_prices / consensus / fundamentals / news_sentiments / investor_trading 일괄 조회
   - score_candidate — 7팩터 가중합산 0~100
   - MA(3) 평활 + 히스테리시스 (entry ≥ 62 / exit < 55)
   - strategy_tag 분류 (RSI≤35 → MEAN_REVERT_RSI, EPS↑5% → EARNINGS_DRIFT, else → SECTOR_MOMENTUM)
       ↓
[Strategy Engine] (결정론 9단계 안전 게이트)
   conviction_floor / macro_closed / duplicate_today / recent_stoploss_cooldown
   today_exit_cooldown / sector_cap / size_below_min / deprecated_tag / unknown_tag
       ↓
[Publisher] (DB upsert + 시트 발행)
   - position_sheets PG upsert
   - STOP 중이면 emit 만 차단(분석 데이터는 보존) — 평소엔 Redis Stream emit
   - Coordinator 이벤트 발행
```

### 2. Fast Loop — 포지션 시트 집행

```
[Position Sheet Consumer] (consumer group: executor)
       ↓
[BalanceAwareSizer] (KIS 잔고 조회 → final_pct 적용 → 주문 수량 계산)
       ↓
[KIS Gateway] (REST 주문 + WebSocket 실시간 체결)
       ↓
[Order Tracking] → executions 테이블
       ↓
[Exit Evaluator] (틱 수신마다 rules[] first_match 평가)
       ↓
[KIS Gateway] (시장가/지정가 청산)
       ↓
outcomes 테이블 (pnl, exit_reason, holding_minutes)
```

### 3. 결정론 Scout 채점 — 7팩터 quant 코어 (2026-05-22 복원)

`prime_jennie_runtime/slow_loop/scout/quant.py` (v2 포팅) 가 종목별 7개 팩터 점수를 합산해 100점 만점으로 산출:

| 팩터 | 최대점 | 핵심 계산 |
|---|---|---|
| **모멘텀** | 20 | RSI(14) 구간점수 + 6M/3M/1M 모멘텀 + 눌림목 보너스 + EPS 상향수정 |
| **품질** | 20 | 선행/실현 ROE 버킷 + 섹터내 PBR/PER 백분위 |
| **가치** | 20 | 섹터내 PER 백분위 + PBR 백분위 + 52주고점 대비 drawdown |
| **기술** | 10 | MA5/MA20 정배열 + 거래량 5일/20일 비율 |
| **뉴스** | 10 | `news_sentiments` 14일 평균 → linear_map |
| **수급** | 20 | 외국인·기관 순매수 + 외인비율 추세 |
| **섹터모멘텀** | 10 | 섹터 평균 20영업일 수익률 → linear_map |

**MA 평활 + 히스테리시스** (`selection.py`):
- `daily_quant_scores` 테이블에 매 run 의 후보별 서브점수 upsert
- 현재 + 직전 2회 run = 3회 평균 점수로 평활 (MA_WINDOW=3, env `SELECTION_MA_WINDOW`)
- 신규 진입: MA ≥ 62 (env `SELECTION_ENTRY_THRESHOLD`)
- 기존 유지: MA ≥ 55 (env `SELECTION_EXIT_THRESHOLD`) — 55~62 구간에서 직전 선정 종목은 유지

**strategy_tag 분류**:
- RSI(14, 15일 이상 일봉) ≤ 35 → `MEAN_REVERT_RSI`
- 컨센서스 EPS 상향수정 ≥ 5% → `EARNINGS_DRIFT`
- 그 외 → `SECTOR_MOMENTUM`
- `GAP_UP_REBOUND` 는 일봉 결정론 코어가 장중 갭 정보를 못 보므로 현재 미부여

선정 경로 전체에서 **LLM 호출 0회**. `scout_runs.model_used` / `cost_usd` 는 항상 NULL.
`scout_runs.prompt_version` 자리에 스코어러 버전 마커 (`deterministic-quant-v2-port@1`).

Observer 이벤트:
- `pj.scout.code_generated` — 결정론 scout 완료 (이벤트명은 LLM codegen 시절 잔존, deprecated 명명)
- `pj.scout.fallback_use_previous_run` — 24h 내 동일 macro state 의 직전 후보 재사용 fallback
- `pj.scout.hallucination_suspected` — universe 밖 ticker (결정론 코어에선 발생 불가, 안전망)
- `pj.scout.no_candidates` — 후보 0

### 4. News Pipeline (Korean + Global)

| 파이프라인 | 소스 | 추출 | 저장 |
|----------|------|-----|------|
| **news-pipeline** (한국) | Naver/네이버 금융 뉴스 (10분 주기) | EXAONE/Qwen3 메타데이터 추출 (impact_level, event_types, sentiment_score) | `news_events` 테이블 + Qdrant 벡터 |
| **news_pipeline_global** (글로벌) | WSJ/Bloomberg/Reuters RSS (2h 주기) | DeepSeek 요약 + Macro digest | `global_macro_news_articles` + `digests` |

Scout 피드는 score 평균이 아니라 **임팩트 × 이벤트 종류 분포**(`events_by_impact`, `positive_events`, `risk_events`)로 LLM에 전달. POSITIVE = `{earnings, contract, product, investment, shareholder_return}`, RISK = `{geopolitical, regulation, strike, lawsuit, bankruptcy}`.

### 5. Exit Rules v3 (9-rule)

`fixed_sl` + `time_stop` 2종 필수, 나머지 7종은 선택. 배열 순서대로 first_match 평가.

```
[overextension_exit] 과열 최우선 (RSI > 85)
        ↓
[profit_floor]       큰 수익 바닥 사수 (고점 +15% 후 +10% 바닥)
        ↓
[trailing_tp]        고점 대비 -3.5% 하락 청산
        ↓
[scale_out]          국면별 분할 익절
        ↓
[breakeven]          +3% 도달 후 +0.3% 미만 청산 (청산 안 하고 SL만 상향)
        ↓
[death_cross]        5MA/20MA 하향 교차 + 손실 구간 (일봉)
        ↓
[fixed_sl]           최후 방어 (필수)
        ↓
[time_stop]          eod 또는 hold_days 만료 (필수)
```

상세 명세: [`docs/POSITION_SHEET_SPEC.md`](./docs/POSITION_SHEET_SPEC.md).

---

## 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Prime Jennie Runtime (v3)                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌────────────────┐   ┌────────────────┐   ┌────────────────┐           │
│  │ News Pipeline  │──>│   Qdrant       │   │ Macro Council  │           │
│  │ (Kor + Global) │   │  (벡터 RAG)     │   │ DeepSeek+Opus  │           │
│  └────────────────┘   └────────────────┘   └────────────────┘           │
│         │                                          │                    │
│         v                                          v                    │
│  ┌────────────────┐                       ┌────────────────┐            │
│  │ news_events    │                       │  Macro Gate    │            │
│  │ (Postgres)     │                       │ open / closed  │            │
│  └────────────────┘                       └────────────────┘            │
│         │                                          │                    │
│         └────────────┬─────────────────────────────┘                    │
│                      v                                                  │
│              ┌─────────────────────────────┐                            │
│              │ Slow Loop                   │                            │
│              │  - Macro (LLM + 안전망)     │                            │
│              │  - 결정론 Scout (7팩터)     │                            │
│              │  - Strategy (9 게이트)      │                            │
│              └─────────────────────────────┘                            │
│                      │                                                  │
│                      v                                                  │
│              ┌────────────────┐                                         │
│              │ position_sheets│ (Postgres + Redis Stream)               │
│              └────────────────┘                                         │
│                      │                                                  │
│                      v                                                  │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐             │
│  │ Fast Loop      │─>│  KIS Gateway   │─>│   KIS Open API │             │
│  │ (consumer)     │  │ (WS+REST)      │  │                │             │
│  └────────────────┘  └────────────────┘  └────────────────┘             │
│         │                                                               │
│         v                                                               │
│  ┌────────────────┐  ┌────────────────┐                                 │
│  │  executions    │  │   outcomes     │                                 │
│  │  (Postgres)    │  │   (Postgres)   │                                 │
│  └────────────────┘  └────────────────┘                                 │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│  Control UI (React 19) │ Dashboard FastAPI │ Telegram Bot │ Monitor    │
├─────────────────────────────────────────────────────────────────────────┤
│  Loki (16 stream) + Promtail + Grafana + Cloudflared (Zero Trust)      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 서비스 구성

**프로덕션**: MS-01 호스트. v3 컨테이너 16개 + v2 공유 3개 (vllm-llm / vllm-embed / qdrant).

### Trading Services (profile: full)

| 서비스 | 포트 | 설명 |
|--------|------|------|
| **kis-gateway** | 8080 | KIS OpenAPI 프록시 + WebSocket 실시간 체결 |
| **fast-loop** | - | 포지션 시트 consumer + 주문 집행 |
| **slow-loop** | - | Macro/Scout LLM 주기 runner |
| **dashboard** | 8090 | FastAPI 백엔드 (9 라우터) |
| **monitor** | 8091 | KIS 잔고/포지션 polling |
| **telegram-bot** | 8082 | 제어 명령 + 알림 |
| **job-worker** | - | 27 cron job runner (macro collect / briefing 등) |
| **price-scheduler** | - | KIS 분봉/일봉 적재 |
| **news-pipeline** | - | 한국 뉴스 + EXAONE/Qwen3 감성 + kure embed |
| **control-ui** | 80 | Nginx SPA (별도 리포 [`prime-jennie-control-ui`](https://github.com/youngs7596/prime-jennie-control-ui)) |
| **screening-executor** | - | Scout 코드 격리 실행용 이미지 (`docker run --rm` spawn, build-only) |

### Infrastructure Services (profile: default + observe + tunnel)

| 서비스 | 포트 | 설명 |
|--------|------|------|
| **postgres** | 5432 | 핵심 DB (17 테이블) |
| **redis** | 6379 | 상태/스트림/캐시 |
| **loki** | 3100 | 로그 집계 |
| **promtail** | - | 로그 shipper (docker SD filter로 v2 소스 차단) |
| **grafana** | 3300 | 관찰 대시보드 |
| **cloudflared** | (2000 metrics) | Zero Trust 터널 |

### v2 Shared Services (외부 의존)

| 서비스 | 설명 |
|--------|------|
| **vllm-llm** | EXAONE 4.0 32B AWQ — 로컬 LLM 추론 |
| **vllm-embed** | KURE-v1 — 한국어 임베딩 |
| **qdrant** | 뉴스 RAG 벡터 저장소 |

---

## 기술 스택

### 백엔드
- **Python 3.12+** — 핵심 언어
- **FastAPI** — REST API (Pydantic v2)
- **SQLAlchemy 2.0 (async) + asyncpg** — Postgres 비동기 ORM
- **Pydantic v2 + pydantic-settings** — 도메인 모델 + 환경설정
- **Redis Streams** — 서비스 간 비동기 메시징 (position_sheets, executions)
- **APScheduler** — 27 cron job runner (DB 등록 기반)

### AI / LLM
- **[minyoung-mah](https://github.com/youngs75/minyoung-mah) v0.1.2+** — Multi-Agent Harness (Council orchestration, Observer events, ResiliencePolicy)
- **DeepSeek Cloud** — Strategist + Risk Analyst + Scout primary
- **Claude Opus** — Macro Council Chief Judge
- **EXAONE 4.0 32B AWQ (vLLM 로컬)** — 한국 뉴스 분석
- **Qwen3 (vLLM 로컬)** — 뉴스 메타데이터 추출 (impact_level, event_types, sentiment_score)
- **KURE-v1 (vLLM 로컬)** — 한국어 임베딩
- **Qdrant** — 벡터 저장소 (뉴스 RAG)
- **litellm + langfuse** — provider 추상화 + LLM 관찰성

### 데이터
- **Postgres 16** — 영구 저장소 (17 테이블, 4 DB 역할)
- **Redis 7** — 상태/스트림/캐시
- **KIS Open API** — 시세/잔고/주문
- **Naver Finance / FnGuide** — 시세, 시가총액, 수급, 재무, 컨센서스 크롤링
- **DART** — 공시 수집

### 프론트엔드 (별도 리포)
- **React 19 + TypeScript** — Dashboard UI
- **Vite** — 빌드 도구
- **Tailwind CSS** — 스타일링

### 인프라
- **Docker Compose** — 18+ 서비스 (5 profile: default / full / apps / observe / tunnel)
- **GitHub Actions** — self-hosted runner `ms-01-v3` 자동 배포
- **Loki + Promtail + Grafana** — 로그 + 모니터링
- **Cloudflare Tunnel** — Zero Trust 외부 접근

---

## 프로젝트 구조

```
prime-jennie-runtime/
├─ prime_jennie_runtime/        # 메인 Python 패키지
│   ├─ slow_loop/                 # Macro(LLM) + 결정론 Scout
│   │   ├─ macro/                   # Macro Pipeline + Gate + 결정론 안전망
│   │   ├─ scout/                   # 결정론 quant 코어 (2026-05-22 v2 포팅)
│   │   │   ├─ deterministic_scout.py   # 오케스트레이터 (run_deterministic_scout)
│   │   │   ├─ quant.py                  # 7팩터 스코어러
│   │   │   ├─ enrichment.py             # universe 적재 (price/consensus/fundamentals/news)
│   │   │   ├─ selection.py              # MA 평활 + 히스테리시스
│   │   │   ├─ context_builder.py / validators.py / feeders/
│   │   │   └─ (role.py, prompts.py, code_loop.py — LLM 시절 잔재, 미사용)
│   │   ├─ strategy/                # 9 안전 게이트 + sheet 발행
│   │   ├─ persistence.py / pipeline.py / app.py
│   │   └─ thesis/                  # thesis_aware_hold (재설계 대기)
│   ├─ fast_loop/                 # Entry/Exit (결정론, LLM 금지)
│   │   ├─ tick_loop.py · bar_engine.py · pending_entry.py
│   │   ├─ entry_executor.py · exit_executor.py · exit_evaluator.py
│   │   ├─ persistence.py            # executions/positions/outcomes 기록
│   │   └─ risk_throttle.py · cooldown_check.py · consumer.py
│   ├─ kis_gateway/               # KIS OpenAPI 프록시 (REST + WebSocket)
│   ├─ dashboard/                 # FastAPI 백엔드 라우터
│   ├─ monitor/                   # KIS 잔고/포지션 polling (장 시간 인식)
│   ├─ news_pipeline_kor/         # Naver 뉴스 + Qwen3 메타데이터 → news_events
│   ├─ news_pipeline_global/      # WSJ/Bloomberg/Reuters via Google News + DeepSeek digest
│   ├─ telegram_bot/              # 제어 + 알림 + LLM intent router
│   ├─ jobs/                      # 26 cron job 핸들러 (job-worker 컨테이너)
│   ├─ control/                   # SystemState + ControlCommand consumer
│   ├─ coordinator/               # event_log 아카이브 + advisory 정책 (별도 컨테이너)
│   ├─ council_logging/           # Macro Council 실행 이력
│   ├─ briefing/                  # 일일 브리핑 (17:00 KST Telegram)
│   ├─ backtest/                  # 백테스트 엔진 (fast_loop 청산 재사용 + no-lookahead 가드)
│   ├─ position_sheet/            # 포지션 시트 schema + 9 exit rule + 6 entry condition
│   ├─ screening_executor/        # 격리 샌드박스 (LLM 시절, 현재 dead-path)
│   └─ infra/                     # config, scheduler, heartbeat, redis_streams, llm_stats
├─ migrations/                  # 001~019 SQL
├─ scripts/                     # seed, backfill, ETL, smoke 등
├─ infra/
│   ├─ docker/                    # 서비스별 Dockerfile
│   ├─ promtail/ · grafana/ · loki/ · prometheus/
├─ docs/                        # 설계 문서 + 운영 가이드
│   ├─ prime_jennie_v3_phase0_design.md
│   ├─ POSITION_SHEET_SPEC.md
│   ├─ MACRO_GATE_SPEC.md
│   ├─ SCOUT_CODE_GENERATION.md   # 1차 아키텍처 (LLM 코드 생성, 2026-05-22 폐기 — 역사 자료)
│   ├─ REAL_MODE_MIGRATION_CHECKLIST.md
│   └─ ...
├─ .ai/sessions/                # 세션 핸드오프 기록 (session-YYYY-MM-DD-NNNN.md)
├─ .ai/decisions/               # 아키텍처 결정 기록 (ADR-style)
├─ tests/                       # unit + integration (1405 passed)
├─ reports/                     # backtest 결과 JSON
├─ .github/workflows/           # ghcr.yml (build) + deploy.yml (MS-01)
└─ docker-compose.yml           # 18+ 서비스 / 5 profile
```

---

## 데이터 흐름

```
[News (Korean + Global)] ──┐
                           v
[Macro Council (3-expert)] ──> [Macro Gate] ──> TradingContext (Redis)
                                                     │ size_multiplier
                                                     v
[Scout (LLM Code Gen)] ──> [Screening Executor (Docker)] ──> Candidates
                                  │ 검증 루프 (최대 3회)
                                  v
                          [Strategy Engine]
                                  │ Pydantic 7-단 검증
                                  v
                  position_sheets (Postgres + Redis Stream)
                                  │
                                  v
[Fast Loop Consumer] ──> [BalanceAwareSizer] ──> [KIS Gateway] ──> 주문
                                                       │
                                                       v
                                              executions (Postgres)
                                                       │
                                                       v
                                              [Exit Evaluator]
                                                       │ 틱마다 first_match
                                                       v
                                                  outcomes (Postgres)
                                                       │
                                                       v
                                              [Telegram 알림 + Dashboard]
```

---

## Exit Rules 체계

9개 규칙 (2개 필수 + 7개 선택), 배열 순서대로 first_match 평가. 첫 번째 매칭 규칙이 실행됩니다.

| 순위 | 규칙 | 조건 | 매도 비율 | 비고 |
|------|------|------|----------|------|
| 1 | **overextension_exit** | 1분봉 RSI(14) > rsi_threshold (보통 85) | 100% | 과열 최우선 |
| 2 | **profit_floor** | 고점 +15% 도달 후 +10% 바닥 깨짐 | 100% | v2 검증 (0.15 / 0.10) |
| 3 | **trailing_tp** | 고점 대비 drop_pct 하락 | 100% | activate_pct 후 활성 |
| 4 | **scale_out** | levels [[trigger, portion], ...] 도달 | 15~25% | 부분 청산, 잔여 유지 |
| 5 | **breakeven** | activate_pct 도달 후 floor_pct 미만 | 100% | v2 검증 (+3% / +0.3%). SL만 상향 |
| 6 | **death_cross** | 5MA/20MA 하향 교차 + 손실 ≥ 1% | 100% | 일봉 기준, 09:00 1회 |
| 7 | **fixed_sl** | profit ≤ -pct (보통 -10%) | 100% | **필수**. 최후 방어 |
| 8 | **time_stop** | eod 또는 hold_days 만료 | 100% | **필수**. 15:20 청산 |
| 9 | **fixed_tp** | profit ≥ pct | 100% | (선택, 단순 익절) |

상세 명세 + Edge case 카탈로그: [`docs/POSITION_SHEET_SPEC.md`](./docs/POSITION_SHEET_SPEC.md) §5, §8.

위 9-rule 은 가격 기반 결정론 exit. **thesis-aware hold** (산 이유가 깨졌으니 가격 무관 매도) 는 별도 layer — 아래 §"Slow Loop Awareness 가드" 참조.

---

## Slow Loop Awareness 가드 (2026-05-17 단순화)

2026-05-15 사고 (-3.35M) 진단 = "slow_loop 정보 결손 + hold/exit 결정의 시장 무관성" → 결정 3 가지 = 가드 3 카테고리.

| 카테고리 | 가드 | 위치 | 상태 |
|---|---|---|---|
| **Awareness** | `outcome_feedback` | Scout context + prompt — `scout_outcomes_v1` view + `ScoutContext.previous_outcomes` | DONE 2026-05-15 |
| **Cooldown** | `same_day_cooldown` | Strategy Engine sheet 발행 3c — 같은 거래일 청산 후 재진입 차단 (익절/손절/manual 무관) | DONE 2026-05-15 |
| **Hold-thesis** | `thesis_aware_hold` | Scout `ThesisSpec` (catalog 5종 condition) + slow_loop revaluator + fast_loop `forced_liquidation:thesis` | Phase A 진입 2026-05-17 |
| [별개 트랙] | 시초 timing (gap_down_block / open_5min_hold) | fast_loop entry/exit | backlog |
| [Deprecated] | `overextension_entry_guard` (구 G2) | — | Pre-flight 부정으로 폐기 2026-05-17 |

**진행 상태**:
- Awareness + Cooldown 작동 중 (2026-05-15 도입 후 운영 검증)
- `thesis_aware_hold` Phase A: `ProvenanceSection.thesis_spec` 영속 + Scout prompt v0.8 (catalog 가이드). revaluator + forced_liquidation 트리거는 Phase 1 (5-22~5-29 advisory) + Phase 2 (5-29~ enforce) 예정.

**catalog 5종** (Phase A 측정 후 확장):
- `kospi_gate` (macro 종합)
- `sector_momentum_above` (섹터 N일 누적)
- `no_risk_event_high` (24h high-impact risk 부재)
- `earnings_event_window` (earnings event 후 N영업일)
- `rsi_below` (1일봉 RSI)

상세: [`.ai/designs/2026-05-17-g-series-simplification.md`](./.ai/designs/2026-05-17-g-series-simplification.md). Pre-flight 분석 산출물 (`overextension_entry_guard` 폐기 근거 포함): [`.ai/analyses/`](./.ai/analyses/).

---

## 리스크 관리

| 기능 | 설명 |
|------|------|
| **Macro Gate** | 매일 1회 + 장중 갱신. `gate=closed`면 Scout 미실행, 신규 진입 0 |
| **size_multiplier** | Macro Gate 출력. 모든 시트 sizing에 곱연산 적용 |
| **risk_multiplier** | Intraday Risk Throttle 시점 스냅샷 (시트 발행 후 고정) |
| **MIN_POSITION_PCT** | `final_pct < 0.005` 시 시트 미발행 (수수료 비효율) |
| **base_pct 상한** | 단일 종목 10% 초과 금지 (`0 < base_pct <= 0.10`) |
| **schema validator (7단)** | sheet_id 포맷 / 시각 일관성 / size 일관성 / entry / exit (fixed_sl + time_stop 필수) / strategy_tag enum / provenance |
| **DLQ** | 검증 실패 시 `position_sheets.dlq` 스트림. 자동 재처리 없음 |
| **idempotency** | 같은 `sheet_id` 두 번 받아도 한 번만 처리 (Postgres unique + Redis processed_set) |
| **Cooldown** | 손절/매도 후 재진입 차단 (Redis 기반) |
| **Universe 검증** | Scout 환각(universe 밖 ticker) 감지, 30% 초과 시 `pj.scout.hallucination_suspected` |
| **강제 청산** | 텔레그램 `/liquidate` 2단계(Add → Arm) emergency stop 우회 |
| **trading_flags:stop** | Redis 키. `stop=1`이면 fast-loop가 신규 진입 모두 차단 |

---

## 설정

환경변수 기반 설정 (Pydantic Settings, env prefix 자동 매핑).

| Prefix | Config Class | 예시 |
|--------|-------------|------|
| `POSTGRES_` | PostgresConfig | `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB` |
| `REDIS_` | RedisConfig | `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` |
| `KIS_` | KISConfig | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_IS_PAPER`, `KIS_BASE_URL` |
| `LLM_` / `DEEPSEEK_` / `ANTHROPIC_` | LLMConfig | `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` |
| `VLLM_` | VLLMConfig | `VLLM_LLM_URL`, `VLLM_EMBED_URL` |
| `QDRANT_` | QdrantConfig | `QDRANT_URL`, `QDRANT_API_KEY` |
| `TELEGRAM_` | TelegramConfig | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `MACRO_` | MacroConfig | `MACRO_AUTO_OVERRIDE_DISABLED` |
| `GRAFANA_` | — | `GRAFANA_PASSWORD` |
| `CLOUDFLARE_` | — | `CLOUDFLARE_TUNNEL_TOKEN` |

전체 설정 목록은 `.env.example`을 참고하세요.

### Docker Compose 프로파일

| 프로파일 | 목적 | 서비스 |
|----------|------|--------|
| `default` | DB만 | postgres, redis |
| `full` | 전체 운영 스택 | 모든 트레이딩 + UI + 관찰성 |
| `apps` | 사용자 대면만 | dashboard, control-ui |
| `observe` | 관찰성만 | loki, promtail, grafana |
| `tunnel` | Cloudflare 터널 | cloudflared |

```bash
# 전체 운영 (운영 동등)
docker compose --profile full up -d

# 개발 (DB만)
docker compose up -d postgres redis

# 관찰성만 추가
docker compose --profile observe up -d
```

---

## 테스트

```bash
# 전체 테스트 (1405 passed / 5 skipped / 2 failed — 실패 2건은 기존 stale)
pytest tests/ -v --tb=short

# Unit 테스트만
pytest tests/unit/ -v

# 통합 테스트 (DB 필요)
pytest tests/integration/ -v

# 특정 모듈
pytest tests/slow_loop/scout/test_code_loop.py -v

# 린트 + 포맷 (커밋 전 필수)
ruff format . && ruff check .
```

---

## 모니터링

### Grafana 대시보드

- URL: `http://localhost:3300` (외부 접근은 Cloudflare Tunnel 경유)
- 기본 계정: admin / `$GRAFANA_PASSWORD`

### 로그 조회

```bash
# 특정 서비스 로그
docker logs prime-jennie-runtime-slow-loop-1 --tail 50

# 시간 범위 + 필터
docker logs prime-jennie-runtime-job-worker-1 --since 2h 2>&1 | grep -E 'macro|error'

# Grafana → Loki 쿼리
{container_name="slow-loop"} |= "scout"
{container_name="fast-loop"} |= "ERROR"
```

### Daemon Heartbeat

각 서비스가 Redis TTL key (`pj:heartbeat:<service>`) 60초마다 갱신. dashboard `/api/system/health`가 종합.

### 운영/디버깅 상세

DB 쿼리, Redis 조회, scheduled_job 관리, KIS API 호출 등은 [`docs/SESSION_HANDOFF_TIMELINE.md`](./docs/SESSION_HANDOFF_TIMELINE.md) 와 세션별 핸드오프(`.ai/sessions/`)를 참조.

---

## 배포 파이프라인

```
push to main → GitHub Actions (self-hosted runner `ms-01-v3`)
             → ssh MS-01
             → cd /home/youngs75/projects/prime-jennie-runtime
             → git pull --ff-only
             → docker compose --profile full up -d --build
```

- workflow 파일: [`.github/workflows/deploy.yml`](./.github/workflows/deploy.yml)
- **로컬에서 MS-01 디렉터리를 직접 수정하지 말 것** — 다음 배포에서 `git stash` 로 보존되지만 git이 source of truth
- 수동 트리거: `gh workflow run deploy.yml --repo youngs7596/prime-jennie-runtime`
- 배포 현황: `gh run list --repo youngs7596/prime-jennie-runtime --limit 5`
- `docs/` 변경 (*.md) 은 `paths-ignore` 로 deploy skip

---

## 문서 인덱스

### 설계
- [`docs/prime_jennie_v3_phase0_design.md`](./docs/prime_jennie_v3_phase0_design.md) — 전체 v3 설계 (v0.3, 일부 1차 아키텍처 잔재 포함)
- [`docs/POSITION_SHEET_SPEC.md`](./docs/POSITION_SHEET_SPEC.md) — 포지션 시트 JSON 스키마 + 9 exit rules · 유효
- [`docs/MACRO_GATE_SPEC.md`](./docs/MACRO_GATE_SPEC.md) — Macro Gate 이진 판정 명세 · 유효
- [`docs/SCOUT_CODE_GENERATION.md`](./docs/SCOUT_CODE_GENERATION.md) — **역사 자료** (1차 아키텍처 LLM 코드 생성, 2026-05-22 폐기). 현재 결정론 코어는 코드 직접 참조: `prime_jennie_runtime/slow_loop/scout/`
- [`.ai/decisions/2026-05-22-selection-architecture-decision.md`](./.ai/decisions/2026-05-22-selection-architecture-decision.md) — LLM-at-core 폐기 결정

### 가드 / 단순화 (2026-05-17)
- [`.ai/designs/2026-05-17-g-series-simplification.md`](./.ai/designs/2026-05-17-g-series-simplification.md) — G 시리즈 명명 폐기, 의미 기반 3 카테고리 재정립 (decision)
- [`.ai/designs/2026-05-14-agent-coordinator.md`](./.ai/designs/2026-05-14-agent-coordinator.md) — Coordinator (State Hub + Decision Authority + Event Bus) design
- [`.ai/designs/2026-05-15-scout-overextension-guards.md`](./.ai/designs/2026-05-15-scout-overextension-guards.md) — G1~G5 통합 design (5-15)
- [`.ai/analyses/`](./.ai/analyses/) — Pre-flight 분석 산출물 (G2 폐기 / G6 catalog coverage)

### 로드맵 / 진행 상황
- [`docs/PHASE2_PLAN.md`](./docs/PHASE2_PLAN.md) — Phase 2 원 계획 (완료)
- [`docs/PHASE_2_10_UTILITIES_INVENTORY.md`](./docs/PHASE_2_10_UTILITIES_INVENTORY.md) — v2→v3 utility 이관 내역
- [`docs/PHASE_2_13_COMPLETE.md`](./docs/PHASE_2_13_COMPLETE.md) — Phase 2.10~2.13 완료 보고서 + Phase 3 경계

### 운영 가이드
- [`docs/REAL_MODE_MIGRATION_CHECKLIST.md`](./docs/REAL_MODE_MIGRATION_CHECKLIST.md) — KIS paper → real 전환 체크리스트
- [`docs/SESSION_HANDOFF_TIMELINE.md`](./docs/SESSION_HANDOFF_TIMELINE.md) — 집중 세션 시리즈 인덱스

### 세션별 원시 기록
- `.ai/sessions/session-YYYY-MM-DD-NNNN.md` — 시점 스냅샷. 가장 최신부터 역순 참조

---

## 관련 리포

| Repo | 역할 | 상태 |
|------|------|------|
| [minyoung-mah](https://github.com/youngs75/minyoung-mah) | Multi-Agent Harness 라이브러리 (consumer extension 패턴) | v0.1.8 (PyPI publish 완료) |
| [prime-jennie-control-ui](https://github.com/youngs7596/prime-jennie-control-ui) | React 19 + Vite 모니터링/제어 UI | Phase 2.11 9 page complete |
| [prime-jennie](https://github.com/youngs7596/prime-jennie) | v2 legacy (vllm/qdrant 공유용) | 유지보수 모드, Phase 6 에서 전면 퇴역 예정 |
| [claude-global-memory (youngs7596)](https://github.com/youngs7596/claude-global-memory) | Claude Code 장기 기억 동기화 | 활성 (MCP bridge 구축 완료, 2026-04-25) |

---

## 기여

- 이슈: GitHub Issues
- PR: `main` 브랜치로. push 시 자동 배포되므로 **반드시 로컬 테스트 후 push**.
- 커밋: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`)
- 린트: 커밋 전 `ruff format . && ruff check .`
- 테스트: 코드 변경 시 관련 테스트 필수. 테스트 없는 커밋 금지.
- 시크릿 절대 커밋 금지 (`.env`, 토큰, 키)
- 자세한 워크 룰: [`AGENTS.md`](./AGENTS.md)

---

<div align="center">

**Prime Jennie Runtime — 결정론 quant 코어 (2026-05-22 복원)**

*결정론이 종목을 고르고, 빠른 루프가 집행한다.*

</div>
