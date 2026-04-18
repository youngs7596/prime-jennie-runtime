# prime-jennie-runtime

Prime Jennie v3 실행 엔진. 자가진화 KOSPI/KOSDAQ 트레이딩 시스템의 핵심 런타임.

**현재 상태**: Phase 2.10–2.13 완료 · Real mode 운영 (실매매) · stop 해제 / 데이터 축적 모드 · 2026-04-18 기준.  
상세 로드맵: [`docs/PHASE_2_13_COMPLETE.md`](./docs/PHASE_2_13_COMPLETE.md) · 세션별 원시 기록: [`docs/SESSION_HANDOFF_TIMELINE.md`](./docs/SESSION_HANDOFF_TIMELINE.md)

## 아키텍처

- **느린 루프** (10~60분 주기): Global News Crawler → Macro Council (Opus + DeepSeek shadow) → Macro Gate 이진 판정 → Scout (LLM 코드 생성) → Screening Executor (격리 Python 실행) → Strategy Engine → 포지션 시트 (Redis Stream)
- **빠른 루프** (초 단위): 포지션 시트 consumer → BalanceAwareSizer → KIS Gateway → 실주문 → `executions` + `outcomes` 기록
- **제어 & 모니터링**: Control UI (React) → FastAPI dashboard → 제어 명령 (stop/pause/dryrun/liquidate) + Telegram 양방향
- **관찰성**: promtail → Loki (16 서비스 stream) + Grafana 대시보드 + daemon heartbeat (Redis TTL key)
- **인터페이스**: 포지션 시트 JSON 스키마 ([`docs/POSITION_SHEET_SPEC.md`](./docs/POSITION_SHEET_SPEC.md))

[minyoung-mah](https://github.com/youngs75/minyoung-mah) Multi-Agent Harness (0.1.2+) 을 소비합니다.

## 런타임 토폴로지

**프로덕션**: MS-01 호스트. v3 컨테이너 16개 + v2 공유 3개 (vllm-llm / vllm-embed / qdrant).

| 서비스 | Profile | 설명 |
|--------|---------|------|
| postgres | (default) | 핵심 DB (17 테이블). port 5432 |
| redis | (default) | 상태 / 스트림 / 캐시. port 6379 |
| kis-gateway | full | KIS OpenAPI 프록시. port 8080 |
| fast-loop | full | 포지션 시트 consumer + 주문 집행 |
| slow-loop | full | Macro/Scout LLM 주기 runner |
| price-scheduler | full | KIS 분봉/일봉 적재 |
| news-pipeline | full | 한국 뉴스 + EXAONE 감성 + kure embed |
| telegram-bot | full | 제어 + 알림 (port 8082) |
| job-worker | full | 27 cron job runner (macro collect / briefing 등) |
| dashboard | full, apps | FastAPI 백엔드 (port 8090, 9 라우터) |
| monitor | full | KIS 잔고/포지션 polling (port 8091) |
| control-ui | full, apps | Nginx SPA (port 80). 별도 리포 [prime-jennie-control-ui](https://github.com/youngs7596/prime-jennie-control-ui) |
| loki | observe, full | 로그 집계 |
| promtail | observe, full | 로그 shipper (docker SD filter 로 v2 소스 차단) |
| grafana | observe, full | 관찰 대시보드 (port 3300) |
| cloudflared | tunnel, full | Zero Trust 터널 (metrics :2000) |
| screening-executor | build-only | Scout 코드 격리 실행용 이미지 (`docker run --rm` spawn) |

**개발 / 로컬**: postgres + redis 만 띄워 단위 테스트. MS-01 에 실제 LLM/KIS 접근 있는 환경이 필요함.

## 배포 파이프라인

```
push to main → GitHub Actions (self-hosted runner `ms-01-v3`)
             → ssh MS-01
             → cd /home/youngs75/projects/prime-jennie-runtime
             → git pull --ff-only
             → docker compose --profile full up -d --build
```

- workflow 파일: [`.github/workflows/deploy.yml`](./.github/workflows/deploy.yml)
- **로컬에서 MS-01 디렉터리를 직접 수정하지 말 것** — 다음 배포에서 git stash 로 보존되지만 git 이 source of truth
- 수동 트리거: `gh workflow run deploy.yml --repo youngs7596/prime-jennie-runtime`
- 배포 현황: `gh run list --repo youngs7596/prime-jennie-runtime --limit 5`

## 로컬 개발 환경

### 설치
```bash
# 가상환경 + 의존성
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# minyoung-mah editable 연결 (0.1.2+ 기능 쓰려면 PyPI publish 전까지 local)
uv pip install -e ../minyoung-mah
```

> **주의**: `uv pip install -e` 는 `uv.lock` 을 변경하지 않음. CI/배포는 `pyproject.toml` 의 git+tag 핀을 resolve 하므로 로컬과 배포 버전이 달라질 수 있음. 자세한 내용은 [`minyoung-mah/docs/CONSUMER_SETUP_FAQ.md`](../minyoung-mah/docs/CONSUMER_SETUP_FAQ.md).

### 인프라 기동 (dev)
```bash
# dev 용 postgres + redis 만
docker compose up -d postgres redis

# 전체 stack (운영과 동일, 리소스 충분 시)
docker compose --profile full up -d
```

### 테스트
```bash
pytest tests/ -v
# 단위 테스트만
pytest tests/unit/ -v
# 통합 테스트 (DB 필요)
pytest tests/integration/ -v
```

## 최초 배포 Run-Book (Bare Metal)

빈 DB 상태에서 처음 올릴 때 순서. 모든 명령은 MS-01 `/home/youngs75/projects/prime-jennie-runtime` 에서.

### Step 1 — 환경 변수
```bash
cp .env.example .env
vi .env  # KIS 계좌 / Telegram / API 키 / Redis/Postgres 비밀번호 등
```

필수 환경변수 카테고리 (비밀값은 이름만):

| 카테고리 | 변수 | 용도 |
|---------|------|------|
| Infra | `POSTGRES_PASSWORD`, `POSTGRES_ADMIN_PASSWORD`, `PJ_RUNTIME_PASSWORD`, `REDIS_PASSWORD` | DB/Cache auth |
| KIS | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `KIS_BASE_URL`, `KIS_IS_PAPER` | 증권사 API + paper/real 플래그 |
| LLM | `VLLM_LLM_URL`, `VLLM_EMBED_URL`, `QDRANT_URL`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` | provider 접근 |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 + 제어 명령 |
| 관찰성 | `GRAFANA_PASSWORD`, `CLOUDFLARE_TUNNEL_TOKEN` | — |
| 운영 플래그 | `MACRO_AUTO_OVERRIDE_DISABLED` (기본 `1` = bypass 활성) | 고변동성 구간 Macro 자동 폐쇄 비활성화. **실매매 재개 전 반드시 `0` 또는 제거** |

### Step 2 — DB 초기화 + 마이그레이션
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
- **001** v3 핵심 스키마 (position_sheets, scout_runs, macro_runs, macro_gates, scheduled_jobs) + 4 DB 역할
- **002** v2 legacy_* 테이블 (ETL 임포트용)
- **003** 시세 (minute_prices, daily_prices)
- **004** 뉴스 (articles, sentiments)
- **005** scheduled_jobs + scheduled_job_runs
- **006** stock_masters, sector_masters
- **007** naver_sectors, us_market_data, investor_trading, foreign_holdings, dart_filings
- **008** 매크로 지표 (macro_indicators, consensus_data, roe_analysis, quarterly_financials)
- **009** v2 거래 이력 (buy_signals, sell_signals, portfolio_snapshots)
- **010** v2 뉴스/거래 보관 테이블
- **011** global_macro_news_articles + digests (Phase 2.13)
- **012** screening_candidates + scout_runs.context_snapshot_json (Phase 2.13 백테스트 재현성)

### Step 3 — Scheduled Jobs Seed
```bash
# 27 cron job 등록 (apscheduler registry)
uv run python -m scripts.seed_scheduled_jobs

# 검증
psql -h localhost -U pj_admin -d prime_jennie_v3 \
  -c "SELECT COUNT(*) FROM scheduled_jobs;"
# 기대: 27 rows
```

주요 job 예시:
- `slow_loop.scout_daily` — `30 8-14 * * 1-5` (8:30~14:30 KST 30분 간격)
- `slow_loop.macro_validate_store` — `30 8 * * 1-5`
- `news_pipeline.crawl_cycle` — `*/10 * * * *` (24/7 10분 주기)
- `price_scheduler.collect_minute` — `*/5 9-15 * * 1-5`
- `job_worker.global_news_crawl` — `0 */2 * * *` (WSJ/Bloomberg/Reuters RSS)
- `job_worker.global_news_digest` — `30 7,11 * * 1-5` (Macro council 30분 전 fresh digest)

DB 는 **cron 표준** (0/7=Sun, 1=Mon). apscheduler 규약 변환은 런타임에 `infra/scheduler.py._normalize_cron_for_apscheduler` 가 처리.

### Step 4 — 전체 스택 기동
```bash
COMPOSE_PROJECT_NAME=prime-jennie-runtime docker compose --profile full up -d --build

# 상태 확인 (약 90초 후)
docker compose --profile full ps

# 헬스 체크
curl http://localhost:8090/api/system/health | python3 -m json.tool
```

### Step 5 — 첫 smoke
```bash
# Balance (paper 모드에서 시작하면 시뮬 계좌)
curl http://localhost:8080/api/balance

# 첫 Scout run 관찰 (scheduled_jobs 에 따라 다음 cron tick 대기)
docker logs prime-jennie-runtime-slow-loop-1 --since 30m | grep scout_daily

# Grafana 관찰 대시보드 (로컬 포트)
open http://localhost:3300   # admin / $GRAFANA_PASSWORD
```

## 운영 모드

### Paper Mode (기본)
- `KIS_IS_PAPER=true` + `KIS_BASE_URL=https://openapivts.koreainvestment.com:29443`
- 시뮬 계좌 — 실매매 0 리스크. 데이터 축적 (시세/뉴스/공시/펀더멘털/수급/지수/미장/AI 로깅) 은 실제 시장 데이터 수집

### Real Mode (실매매)
- `KIS_IS_PAPER=false` + `KIS_BASE_URL=https://openapi.koreainvestment.com:9443`
- **전환 절차는 반드시** [`docs/REAL_MODE_MIGRATION_CHECKLIST.md`](./docs/REAL_MODE_MIGRATION_CHECKLIST.md) **를 Top-down 으로 실행**. Stop flag 선결 → .env 교체 → 토큰 재발급 → Macro bypass 해제 → Stop 해제 순서 엄수

### 제어 명령 (Telegram 또는 API)
- `/stop` — 모든 신규 진입 차단 (기존 청산 유지)
- `/pause <reason>` — 일시 중단 (reason 기록)
- `/resume` — 재개
- `/dryrun` — 시뮬레이션 모드 (주문 전송 대신 로깅)

## 디렉터리 구조

```
prime-jennie-runtime/
├─ prime_jennie_runtime/      # 메인 Python 패키지
│   ├─ slow_loop/               # Macro + Scout (LLM)
│   ├─ fast_loop/               # Entry/Exit (결정론)
│   ├─ kis_gateway/             # KIS OpenAPI 프록시
│   ├─ dashboard/               # FastAPI 백엔드 (9 라우터)
│   ├─ monitor/                 # KIS 포지션/잔고 폴링
│   ├─ news_pipeline/           # 한국 뉴스 수집
│   ├─ news_pipeline_global/    # WSJ/Bloomberg/Reuters (Phase 2.13)
│   ├─ price_scheduler/         # KIS 시세 스케줄러
│   ├─ telegram_bot/            # 제어 + 알림
│   ├─ job_worker/              # 27 cron job runner
│   ├─ council_logging/         # LLM 호출 이력 저장
│   ├─ briefing/                # 일일 브리핑 리포트
│   ├─ backtest/                # 백테스트 엔진 (Phase 3 확장 예정)
│   └─ infra/                   # config, scheduler, heartbeat, llm_stats, redis_streams
├─ migrations/                 # 001~012 SQL
├─ scripts/                    # seed, backfill, ETL, smoke
├─ infra/
│   ├─ docker/                  # Dockerfile.* (10개, 서비스별)
│   ├─ promtail/                # promtail-config.yaml
│   ├─ grafana/                 # dashboards
│   └─ loki/                    # loki-config.yaml
├─ docs/                       # 설계 문서 + 운영 가이드
│   ├─ POSITION_SHEET_SPEC.md
│   ├─ SCOUT_CODE_GENERATION.md
│   ├─ MACRO_GATE_SPEC.md
│   ├─ prime_jennie_v3_phase0_design.md
│   ├─ PHASE_2_13_COMPLETE.md
│   ├─ REAL_MODE_MIGRATION_CHECKLIST.md
│   ├─ SESSION_HANDOFF_TIMELINE.md
│   └─ SESSION_HANDOFF_*.md    # 시점별 세션 기록 6개
├─ tests/                      # unit / integration / e2e
├─ .github/workflows/          # deploy.yml
└─ docker-compose.yml          # 17 서비스 + 5 profile
```

## 문서 인덱스

### 설계 (항상 유효)
- [`docs/prime_jennie_v3_phase0_design.md`](./docs/prime_jennie_v3_phase0_design.md) — 전체 v3 설계 (v0.3)
- [`docs/POSITION_SHEET_SPEC.md`](./docs/POSITION_SHEET_SPEC.md) — 포지션 시트 JSON 스키마 + 9 exit rules
- [`docs/SCOUT_CODE_GENERATION.md`](./docs/SCOUT_CODE_GENERATION.md) — Scout LLM 코드 생성 명세
- [`docs/MACRO_GATE_SPEC.md`](./docs/MACRO_GATE_SPEC.md) — Macro Gate 이진 판정 명세

### 로드맵 / 진행 상황
- [`docs/PHASE2_PLAN.md`](./docs/PHASE2_PLAN.md) — Phase 2 원 계획 (완료)
- [`docs/PHASE_2_10_UTILITIES_INVENTORY.md`](./docs/PHASE_2_10_UTILITIES_INVENTORY.md) — v2→v3 utility 이관 내역
- [`docs/PHASE_2_13_COMPLETE.md`](./docs/PHASE_2_13_COMPLETE.md) — Phase 2.10~2.13 완료 보고서 + Phase 3 경계

### 운영 가이드
- [`docs/REAL_MODE_MIGRATION_CHECKLIST.md`](./docs/REAL_MODE_MIGRATION_CHECKLIST.md) — KIS paper → real 전환 체크리스트
- [`docs/SESSION_HANDOFF_TIMELINE.md`](./docs/SESSION_HANDOFF_TIMELINE.md) — 2026-04-18 집중 세션 시리즈 인덱스

### 세션별 원시 기록 (시점 스냅샷)
- `docs/SESSION_HANDOFF_2026-04-18.md` — Phase 2.10 Agent Teams 12 task closing
- `docs/SESSION_HANDOFF_2026-04-18_p2.11-2.12.md` — Control UI 5 page + Macro Shadow
- `docs/SESSION_HANDOFF_2026-04-18_p2.13.md` — Global news + heartbeat + cloudflared
- `docs/SESSION_HANDOFF_2026-04-18_recovery.md` — UI 공백 + apscheduler dow 함정
- `docs/SESSION_HANDOFF_2026-04-18_real_mode.md` — Backtest persistence + Scout 실전 + Real 전환
- `docs/SESSION_HANDOFF_2026-04-18_logs_and_vllm_fp8.md` — Logs 가시성 + news 24/7 + vLLM FP8 KV

## 관련 리포

| Repo | 역할 | 상태 |
|------|------|------|
| [minyoung-mah](https://github.com/youngs75/minyoung-mah) | Multi-Agent Harness 라이브러리 | v0.1.2 (master push, PyPI 미publish) |
| [prime-jennie-control-ui](https://github.com/youngs7596/prime-jennie-control-ui) | React 19 + Vite 모니터링/제어 UI | Phase 2.11 9 page complete |
| [prime-jennie](https://github.com/youngs7596/prime-jennie) | v2 legacy (vllm/qdrant 공유용) | 유지보수 모드, Phase 6 에서 전면 퇴역 예정 |

## 기여

- 이슈: GitHub Issues
- PR: `main` 브랜치로. push 시 자동 배포되므로 **반드시 로컬 테스트 후 push**.
- `docs/` 변경 (*.md) 은 `paths-ignore` 로 deploy skip
