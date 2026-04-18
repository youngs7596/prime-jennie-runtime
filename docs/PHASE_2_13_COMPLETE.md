# Phase 2.10 → 2.13 완료 보고서 + Phase 3 경계

**업데이트**: 2026-04-18  
**범위**: Phase 2.10 (v2 완전 대체 포팅 + 실전 배치) 이후 Phase 2.13 (자가진화 feeder/infra) 까지의 집약 요약  
**참고**: 세션 시간순 원시 기록은 [SESSION_HANDOFF_TIMELINE.md](./SESSION_HANDOFF_TIMELINE.md) 참고

---

## 1. Phase 2 완성도 스냅샷

| Phase | 마감 | 주요 성과 | 상태 |
|-------|------|---------|------|
| 2.10 | 2026-04-18 새벽 | v2 22 컨테이너 → v3 17 컨테이너, 6 Track 병렬 Agent Teams 로 3시간에 closing | ✅ 완료 |
| 2.11 | 2026-04-18 주간 | Control UI 4 → 9 page. dashboard `/api/scout` 신설 + Macro/Scout/LLMStats/Logs/Overview | ✅ 완료 |
| 2.12 | 2026-04-18 주간 | Macro stub feeder 3종 → real (KOSPI/KOSDAQ/VIX/섹터). DeepSeek Shadow 병렬 (비용 62× 차이 실측) | ✅ 완료 |
| 2.13 | 2026-04-18 야간 | 글로벌 매크로 뉴스 (WSJ/Bloomberg/Reuters), Asia 지수/환율/원자재, minyoung-mah 0.1.2 usage, daemon heartbeat, cloudflared metrics | ✅ 완료 (스킵 2개 제외) |

**Phase 2.13 skip**: 
- 2.13-4 Macro 모델 스위치 결정 (2-3주 shadow 데이터 누적 대기, 2026-05-05 이후)
- 2.13-7 Real mode 체크리스트 (운영 결정 대기)

> **실제 Real 전환**은 Phase 2.13 skip 항목과 별개로 2026-04-18 real_mode 세션에서 수동 실행됨. 체크리스트는 [REAL_MODE_MIGRATION_CHECKLIST.md](./REAL_MODE_MIGRATION_CHECKLIST.md) 로 문서화.

---

## 2. 컴포넌트별 완성도

### 2.1 Slow Loop (Macro + Scout)

| 영역 | 2.10 완료 | 2.11-2.13 보강 | 미완 |
|------|---------|--------------|------|
| Macro Gate | stub feeder + Opus LLM | RealMarketSnapshotFeeder / RealKorMacroNewsFeeder / RealWsjDigestFeeder, DeepSeek Shadow 병렬, Nikkei/HSI/USD-JPY/crude/gold 수집, global_macro_news_digests | Macro 모델 스위치 결정 (shadow 누적 대기), Trading Economics 경제 캘린더 |
| Scout | Python 코드 생성 + 격리 실행 + 후보 인쇄 | Real feeder 4종 (Universe/News/Sector/Market), ScreeningToolAdapter wire, prompt MultiIndex 교정, screening_candidates persistence | Scout prompt few-shot 예제 삽입, engine_error 세분화 (ValidationError 별도 분류), `SCOUT_VALID_UNTIL_BYPASS` env |
| Persistence | scout_runs / macro_runs / macro_gates 3테이블 | migration 012 — scout_runs.context_snapshot_json + screening_candidates (6종 rejection_reason) | 월간 합산 view, backtest 엔진 신설 |

### 2.2 Fast Loop (Entry/Exit)

| 영역 | 2.10 완료 | 2.11-2.13 보강 | 미완 |
|------|---------|--------------|------|
| Stream consumer | position_sheets → KIS 주문 | — | — |
| Stop 이중 차단 | Redis trading_flags:stop + position_sheets.valid_until | real_mode 세션에서 5 layer 검증 (stream ACK-first, duplicate_today, BalanceAwareSizer) | 매수 신호 Telegram 알림 (stop 과 무관한 정보성) |

### 2.3 Control UI

| 영역 | 2.10 완료 | 2.11-2.13 보강 | 미완 |
|------|---------|--------------|------|
| 페이지 | Portfolio / Trades / Jobs / System (4) | Overview / Macro / Scout / LLMStats / Logs (5 신규 = 총 9) | 실인증 (현재 X-User 헤더 기반 감사만) |
| 차트/카드 | 기본 — | Macro Timeline, Shadow 비교 카드 (gate/size/cost ratio), Scout candidates 테이블 (stock_masters LEFT JOIN) | Scout Detail 의 factor_weights 바 차트, LLM Stats 일별 stack bar |
| 안정성 | 기본 SPA | nginx resolver 동적 DNS (컨테이너 재생성 시 502 방지), BigInt 기반 nanosecond timestamp 처리 | — |
| 타입 계약 | hand-mirror (inline `src/lib/api.ts`) | — | OpenAPI codegen 전환 (Phase 2.11 backlog, >40 endpoint 넘어가면 검토) |

### 2.4 Infrastructure

| 영역 | 2.10 완료 | 2.11-2.13 보강 | 미완 |
|------|---------|--------------|------|
| Scheduler | apscheduler + scheduled_jobs DB + 27 job seed | cron dow 규약 변환 (`_normalize_cron_for_apscheduler`, DB 는 cron 표준 유지), news_pipeline 24/7 10분 주기 | heartbeat interval/TTL env 변수화 |
| 관찰성 | Loki + promtail + Grafana + dashboard /system/health | daemon heartbeat (5 daemon, Redis TTL key), cloudflared `--metrics :2000`, Loki 라벨 `{service=...}`, dashboard docker_sd_configs filters (v2 컨테이너 소스 차단) | - |
| 배포 | 수동 scp + docker compose | GitHub Actions self-hosted runner `ms-01-v3` + `.github/workflows/deploy.yml` (push to main → MS-01 자동 배포) | svc.sh systemd 영구화 (현재 tmux detached) |
| 네트워크 | v3 bridge + v2 host-mode 혼재 | v2 qdrant/vllm bridge 전환 + `runtime_shared: external` 네트워크 공유 | — |

### 2.5 Data Pipeline

| 영역 | 2.10 완료 | 2.11-2.13 보강 | 미완 |
|------|---------|--------------|------|
| 한국 뉴스 | news-pipeline → EXAONE 감성 + kure 임베딩 | 10분 주기 24/7 확장 | — |
| 글로벌 뉴스 | — | `news_pipeline_global/` 신규 패키지 (WSJ/Bloomberg/Reuters via Google News) + DeepSeek LLM 6-10줄 digest + migration 011 | Nikkei 전용 피드, Trading Economics 등 |
| 한국 지수/섹터 | macro_indicators / naver_sectors 등 10+ 테이블 | — | — |
| 아시아/원자재 | US market daily 만 | Yahoo Finance chart API — `^N225` / `^HSI` / `JPY=X` / `CL=F` / `GC=F` | 추가 원자재 (은/구리), 실시간 환율 |
| KIS 시세 | 분봉/일봉 스케줄러 | universe daily_prices backfill (KIS 60일, 4590 rows/153 ticker) | — |

### 2.6 LLM 스택

| 영역 | 2.10 완료 | 2.11-2.13 보강 | 미완 |
|------|---------|--------------|------|
| Orchestrator | minyoung-mah 0.1.0 (6 protocol + StaticPipeline) | 0.1.2 usage metadata 전파 → cost 실측. v3 consumer 의 `persistence._resolve_cost` 가 3-우선순위 fallback (cost_usd → usage × pricing → char 추정) | minyoung-mah PyPI publish (사용자 액션 대기) |
| Provider 라우팅 | tier → provider 매핑 | `TieredModelRouter` 의 shadow_reasoning (DeepSeek) 추가, `DEEPSEEK_SHADOW_MODEL` env, deepseek-reasoner(R1) json_mode wrapper | — |
| vLLM | EXAONE-4.0-32B-AWQ fp16 KV | fp8_e4m3 KV + max-num-seqs 128 → KV 20,368 tokens, 4.96× concurrency, 메모리 75% 절감 | TurboQuant 평가 (nightly 이미지 + Korean eval harness) |

---

## 3. 런타임 컨테이너 인벤토리 (2026-04-18 기준)

**v3 prime-jennie-runtime (16 + 5 = 21 containers)**

| 서비스 | Profile | 역할 | 최근 상태 |
|--------|---------|------|----------|
| postgres | (default) | 핵심 DB | Up 27h healthy |
| redis | (default) | 상태/스트림/캐시 | Up 22h healthy |
| kis-gateway | full | KIS OpenAPI 프록시 (port 8080) | Up 4h |
| fast-loop | full | entry/exit 실행자 | Up 9h |
| slow-loop | full | Scout/Macro LLM runner | Up 3h |
| price-scheduler | full | KIS 시세 적재 | Up 9h |
| news-pipeline | full | 뉴스 + EXAONE + kure embed | Up 9h |
| telegram-bot | full | 제어 + 알림 | Up 21h |
| job-worker | full | 27 cron job runner | Up 9h |
| dashboard | full, apps | FastAPI 백엔드 (port 8090) | Up 4h |
| monitor | full | KIS 폴링 + 포지션 동기화 | Up 10h |
| control-ui | full, apps | Nginx SPA (port 80) | Up 4h |
| loki | observe, full | 로그 저장 | Up 22h |
| promtail | observe, full | 로그 shipper | Up 1h (최근 filters 수정) |
| grafana | observe, full | 관찰 대시보드 (port 3300) | Up 21h |
| cloudflared | tunnel, full | Zero Trust 터널 | Up 10h |
| screening-executor | build-only | ephemeral 빌드만 | (이미지만 존재) |

**v2 공유 (prime-jennie-, 설계대로 잔존 3개)**

| 서비스 | 용도 |
|--------|------|
| prime-jennie-vllm-llm-1 | EXAONE-4.0-32B-AWQ (fp8 KV) |
| prime-jennie-vllm-embed-1 | kure-v1 임베딩 |
| prime-jennie-qdrant-1 | 뉴스 벡터 DB |

v3 는 `runtime_shared` 외부 네트워크 + container name 기반으로 접근 (`http://prime-jennie-vllm-llm-1:8001/v1` 등).

**v2 퇴역됨 (2026-04-18 real_mode 세션 + 재발견 정리)**:
legacy 12 컨테이너 (price-monitor/buy-scanner/scout-job/sell-executor/buy-executor/airflow 2/kis-gateway/dashboard/dashboard-frontend/news-pipeline/telegram/job-worker) — 재부팅 시 restart policy 로 부활할 수 있음. 발견 시 `docker update --restart=no → stop → rm` 반복 필요.

---

## 4. 운영 정책 현황

### 4.1 거래 모드
- **현재**: **Real (실매매)**. 2026-04-18 real_mode 세션에서 paper → real 전환 완료.
- **계좌**: 68211289 (KIS real 계좌). BASE_URL=openapi.koreainvestment.com:9443.
- **stop 상태**: `control.state:stop=1` 유지 중 — 자동 해제 경로 없음. 사용자가 명시적으로 `SET 0` 해야만 풀림.
- **MACRO_AUTO_OVERRIDE_DISABLED**: `1` (bypass 활성) — 데이터 축적 기간 용도. 실매매 재개 전 **반드시 `0` 또는 제거**.

### 4.2 데이터 축적 모드
- Scout/Macro 모두 real feeder 기반. matters:
  - 매 평일 08:30~14:30 KST 30분마다 Macro 7 runs + Shadow 7 runs
  - 매 평일 09:30~14:30 KST 1시간마다 Scout 6 runs
  - Global news crawl 2시간 주기 (24/7), digest 30분 전 Macro Council 에 의존
  - 한국 뉴스 10분 주기 (24/7, 장외 포함)

### 4.3 관찰성
- Logs UI: Loki 로 16 v3 서비스 stream 정상. promtail drop 0/s (docker_sd_configs filters 로 v2 소스 차단).
- Heartbeat: 5 daemon (slow/fast/news/price/job-worker) Redis TTL key 갱신. dashboard 가 "heartbeat 7~9s ago" 메시지.
- Cloudflared: `/ready` probe OK. 4 tunnel connection.

### 4.4 배포
- GitHub Actions self-hosted runner `ms-01-v3` 등록 (v2 전용 runner 와 병렬).
- push to main → MS-01 에서 `git pull --ff-only` + `docker compose --profile full up -d --build` 자동 실행.
- 로컬에서 직접 MS-01 scp 하지 말 것 — 다음 배포에서 stash 로 보존되지만 git 경로가 source of truth.

---

## 5. Phase 3 경계 — 자가진화 코어 (proposed)

Phase 2.x 는 "v2 대체 포팅 + 운영 안정화" 축이었다. Phase 3 은 **자가진화 본격 전개** — 수집한 데이터를 실제 개선 루프에 투입.

### 5.1 Phase 3 의 목표 (사용자 방향성 기준)

| Pillar | 의도 | Phase 2 잔여 의존 |
|--------|------|----------------|
| **P3-Backtest Engine** | screening_candidates + context_snapshot + code_text 로 과거 시점 재현 실행 + 성과 평가 | 2-3주 candidates 데이터 누적 (2026-05-05~ 충분) |
| **P3-Scout Evolution** | 실거래 outcomes 피드백을 Scout prompt 로 반영 (few-shot / RL) | backtest 엔진 선행 |
| **P3-Macro 스위치** | 2-3주 shadow 데이터로 Opus vs DeepSeek RMS 비교 → 저비용 모델로 교체 결정 | 2026-05-05~ |
| **P3-Meta** | 다른 domain 에도 같은 구조 적용 (prime-jennie-meta 리포 — 미생성) | Phase 3 핵심 진입점 |

### 5.2 Phase 3 진입 전 선행 조건

- **minyoung-mah PyPI publish** (0.1.2): cost 실측 값 승격. 사용자 `cd ~/projects/minyoung-mah && uv build && uv publish` 필요
- **Backtest 데이터 임계량**: 평일 × 7 scout run × ~20 candidates = ~140 rows/day. 2-3주 누적 시 ~2000~3000 rows → 엔진 구현 가치 유의
- **Real mode 안정성 입증**: stop 해제 후 1-2주 무사고 운영 (체결 오차 없음, 주문 reject 없음)
- **문서 정리 완료**: 본 문서 + TIMELINE + ARCHITECTURE + CONSUMER_SETUP_FAQ

### 5.3 Phase 3 에서 다루지 않는 것

- Phase 2 에서 완료한 모든 컴포넌트 (fast/slow loop 엔진, UI 9 page, LLM 스택, data pipeline 17 테이블)
- v2 유지보수 — Phase 6 에서 v2 완전 퇴역 시점에 재검토

---

## 6. 주요 커밋 체인 (Phase 2.10 ~ 2.13)

### Phase 2.10 (2026-04-18 새벽) — 12 Track closing
`eaf6302 → 875954e → 70c8b0d → 100299b → fb119f1 → 92cc428`

### Phase 2.11 (2026-04-18 아침~주간) — Control UI
`dfa9263 → 8ed49bd → 45c856d → d0cadc9 → 56b4a46` + control-ui `a462756 → 500e216 → 2987076`

### Phase 2.12 (2026-04-18 주간) — Macro feeder + Shadow
`56b4a46 → d61cc18 → 446107f → d83c246 → bd6ad33 → d752b96`

### Phase 2.13 (2026-04-18 야간) — 자가진화 feeder/infra
`aa61879 → 18dc872 → 50c4b55 → 1437c5f → a20d748 → e41e81c → 6b2e14a`  
Upstream: minyoung-mah `ec31e92` (v0.1.2)

### Recovery + Real mode (2026-04-18 오후~저녁) — UI 공백 + v2 퇴역 + real 전환
runtime `1c30390 → 2f308ae → 5676496 → 64d0d6d → c2d9422 → 2f742c7 → a6d6c44 → 10d6d7e → 03a7cd8 → 51c38df`  
control-ui `4e8d7b4 → 9f88136 → a1308d7 → d03afb4 → e462df1`  
v2 compose `5ca4dee`

### Logs/vLLM + 배포 파이프라인 (2026-04-18 저녁~밤)
`52a69f6 → 842784c → 57990a5 → 8be4786 → 7e4ac44 → f43015b`

---

## 7. 다음 세션 즉시 체크

```bash
# 1. 현재 main 상태 + 배포 파이프라인
cd ~/projects/prime-jennie-runtime && git log --oneline -10
gh run list --repo youngs7596/prime-jennie-runtime --limit 3

# 2. MS-01 v3 runtime 헬스 (16 서비스)
ssh prime-jennie 'docker ps --format "{{.Names}}\t{{.Status}}" | grep prime-jennie-runtime'

# 3. v2 legacy 찌꺼기 검증 (vllm/qdrant 3개 이외 있으면 재청소)
ssh prime-jennie 'docker ps -a --format "{{.Names}}" | grep -E "^prime-jennie-" | grep -v "^prime-jennie-runtime-" | grep -v -E "^prime-jennie-(vllm|qdrant)"'
# 결과가 비어있어야 정상

# 4. Real mode stop flag (0 이면 실매매 허용 상태 — MACRO_AUTO_OVERRIDE 점검 필수)
ssh prime-jennie 'docker exec prime-jennie-runtime-redis-1 redis-cli -a <PW> GET control.state:stop'

# 5. minyoung-mah PyPI 버전
pip index versions minyoung-mah  # 또는 pypi.org 확인
```
