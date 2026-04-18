# Session Handoff Timeline

2026-04-17 ~ 2026-04-18 집중 세션 시리즈의 시간순 인덱스. 핸드오프 파일명만으로는 선후/의존을 추적하기 어려워 이 문서로 목차화한다.

> **읽는 법**: 각 항목은 `파일 — 범위 — 시점` 순. 아래로 갈수록 최신. 세션 간 의존이 있으면 "선행" / "후속" 링크로 명시한다.

---

## 0. 직전 상태 (2026-04-17 늦은 저녁 이전)

Phase 2.9 slice2/slice3 미완 + v2 (prime-jennie) 22 컨테이너 + v3 (prime-jennie-runtime) 는 부분 포팅 + dashboard/monitor/control-ui 미착수. Agent Teams 투입 직전.

---

## 1. `../.ai/sessions/session-2026-04-18-0001.md` — Phase 2.10 실전 배치

**시점**: 2026-04-17 늦은 저녁 ~ 2026-04-18 새벽  
**팀**: lead + 6 Opus teammate (Agent Teams experimental)  
**총 task**: 12/12 closing

핵심:
- 6 Track 병렬로 3시간 만에 v2→v3 전면 포팅 (job-worker 25 handler / migrations 006-010 / dashboard 8 routers / monitor / briefing / council_logging / backtest / utilities 인벤토리 / control-ui repo 신설 / compose 15 서비스 통합 / MS-01 cutover)
- v2 22 컨테이너 → v3 17 컨테이너 단독 운영 (+ v2 공유 vllm/qdrant 3개 잔존)
- **운영 모드 = paper 유지** (KIS_IS_PAPER=true). 실매매 0 리스크
- Teams 운영 교훈: git worktree 격리 필수, bypassPermissions 모드 영구 박음, lead 는 `git branch --show-current` 선행 확인 고정
- 첫 커밋 체인: `eaf6302 → 875954e → 70c8b0d → 100299b → fb119f1 → 92cc428`

**Phase 2.11 후보로 이월된 항목**: Macro/Scout/LLMStats/Logs/Overview 페이지 (control-ui), daemon heartbeat, cloudflared metrics probe, real mode 체크리스트.

---

## 2. `../.ai/sessions/session-2026-04-18-0002.md` — Control UI 보강 + Macro Shadow

**시점**: 2026-04-18 아침 ~ 주간 (Phase 2.10 세션 종료 직후 이어 접속)  
**팀**: lead 단독  
**선행**: Phase 2.10 backlog

### Phase 2.11 — Control UI 5 신규 페이지
- dashboard `/api/scout` 신설 (runs / latest / dates / detail 4 endpoint + 3 tests)
- frontend: 4 page → **9 page** (신규 Overview / Macro / Scout / LLMStats / Logs)
- Overview 5 KPI 카드 + Macro Timeline / Recent Scout / Recent Trades
- Macro Detail: Top Risks 색상 severity, Council Log Steps, shadow 비교 카드
- slow-loop env 에 `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` pass-through
- Dockerfile 10개 `infra/docker/` 이동

### Phase 2.12 — Macro feeder 실배선 + DeepSeek Shadow
- stub 3종 → 실데이터 feeder: `RealMarketSnapshotFeeder` (KOSPI/KOSDAQ/VIX/USD-KRW + SP500/NASDAQ + 섹터 drops), `RealKorMacroNewsFeeder` (news_articles × sentiments 24h), `RealWsjDigestFeeder` (us_market_daily factual digest)
- `asyncio.gather` 로 Primary (Opus) + Shadow (DeepSeek) 병렬 실행
- **비용 62× 차이 실측**: Opus $0.0408 / run vs DeepSeek $0.00065 / run
- 매 평일 08:30~14:30 KST 30분마다 7회 × primary+shadow 자동 수집 시작

### Addendum — LLM Features 정정 + DeepSeek 모델 계보
- `LLMConfig` drift 수정 (dashboard Features 표가 실제 라우팅과 어긋남)
- **DeepSeek `-chat`** = V3.2 flagship (apples-to-apples with Opus)
- DeepSeek R1 (`-reasoner`) tool_choice 미지원 → json_mode wrapper 추가 후 default 는 `-chat` 유지

**후속**: Phase 2.13 후보 7건 (WSJ 실 크롤러 / 아시아 지수·환율 수집 / minyoung-mah usage patch / Macro 스위치 결정 / daemon heartbeat / cloudflared metrics / real mode 체크리스트).

---

## 3. `../.ai/sessions/session-2026-04-18-0003.md` — 자가진화 feeder/infra 보강

**시점**: 2026-04-18 늦은 오후 ~ 야간 (p2.11/2.12 addendum 직후)  
**팀**: lead 단독  
**선행**: Phase 2.12 후보 리스트  
**스킵**: 2.13-4 (Macro 스위치, 데이터 대기), 2.13-7 (real mode 체크리스트, 사용자 결정 대기)

완료 항목:
- **2.13-1**: 신규 패키지 `news_pipeline_global/` — WSJ/Bloomberg/Reuters RSS → DeepSeek LLM digest. migration 011 (`global_macro_news_articles` + `digests`). Scheduler job 2개 (`global_news_crawl 0 */2 * * *` + `global_news_digest 30 7,11 * * 1-5`). **WSJ 공식 RSS 2025-01-27 retired** 발견 → Google News site-filter 경유로 교체
- **2.13-2**: Yahoo Finance chart API 재사용해 `^N225` / `^HSI` / `JPY=X` / `CL=F` / `GC=F` 수집. `check_closed_conditions` 바이너리 룰은 참조 안 하므로 gate 거동 불변, Macro LLM prompt context 만 풍부해짐
- **2.13-3**: minyoung-mah upstream `ec31e92` (v0.1.2) — `AIMessage.usage_metadata` 누적 → `RoleInvocationResult.metadata`. consumer `persistence._resolve_cost` 가 usage dict × `_PRICING` 우선, char 추정은 fallback. **PyPI publish 는 사용자 액션 대기**. publish 즉시 자동 승격 (코드 재배포 불필요)
- **2.13-5**: `infra/heartbeat.py` 신규. 5 daemon (slow/fast/news/price/job-worker) 이 Redis `heartbeat:{service}` key (interval 30s / TTL 90s). dashboard `_check_daemon` 이 heartbeat 우선 → 없으면 docker state fallback. "container running + heartbeat 없음 = `unhealthy` (loop deadlock 의심)"
- **2.13-6**: cloudflared `--metrics 0.0.0.0:2000` (127.0.0.1 바인드). dashboard `_DEFAULT_TARGETS` 에 추가 → UI 가시화
- **Ad-hoc**: Loki 쿼리 라벨 `{app=...}` → `{service=...}` (v2 포팅 누락 버그)

**후속 (2.13 잔여 + 2.14 후보)**: Macro 모델 스위치 결정 (2026-05-05 이후), real mode 전환 체크리스트, minyoung-mah PyPI publish, heartbeat interval/TTL env 변수화, Trading Economics 경제 캘린더.

---

## 4. `../.ai/sessions/session-2026-04-18-0004.md` — UI 공백 진단 + 월요 스케줄러 함정

**시점**: 2026-04-18 오후 (Phase 2.13 직후 재접속)  
**팀**: lead 단독  
**선행**: Phase 2.13 배포 상태  
**중대성**: ★ — 월요일(2026-04-20) 장 시작 전 apscheduler dow 함정 잡음. 안 잡혔다면 macro/scout/minute_chart 전량 누락 예정

이슈 맵 (5 + 1):
1. control-ui → `/api/*` **502 Bad Gateway** — nginx startup DNS 영구 캐시. `resolver 127.0.0.11 valid=10s` + `set $upstream + proxy_pass $var$uri` 로 요청마다 재해결
2. monitor 전량 404 + Portfolio 잔고 공백 — `/balance` → `/api/balance` 경로 불일치 (KIS Gateway 실제 라우트)
3. Logs `Invalid Date` — Loki nanosecond timestamp 를 `new Date()` 가 NaN. `BigInt(ts) / 1_000_000n` 로 ms 환산
4. news-pipeline kure embed + EXAONE 전량 실패 — `.env` VLLM/QDRANT URL 이 `127.0.0.1` (v2 host-mode 잔재). v3 bridge 에선 자기 자신. **ufw INPUT default deny 로 차단**
5. v2 host-mode vs v3 bridge L2 단절 — 근본 원인 (4번의 상위)
6. **CronTrigger.from_crontab 의 dow 해석 ≠ cron 표준** — cron 표준은 0/7=Sun/1=Mon, apscheduler 는 0=Mon/6=Sun. `1-5` → **화-토**로 오해석. Phase 2.9 slice2 이후 전부 하루 밀림

핵심 수정:
- `infra/scheduler.py._normalize_cron_for_apscheduler` — DB 는 cron 표준 유지, 런타임에만 변환 (단일/범위/리스트/스텝/이름 mon-fri 전부 지원)
- v2 compose `qdrant/vllm` bridge 전환 + `runtime_shared: external` 네트워크 공유. `.env` VLLM/QDRANT URL 을 container name 기반으로 교정
- **이중 검증 실측**: 토요일 13:35 KST tick 에서 `scheduled_job_runs 0 rows` — 더 이상 토요일 실행 안 함

커밋: runtime `1c30390 + 2f308ae`, control-ui `4e8d7b4 + 9f88136`, v2 compose `5ca4dee` (development).

**후속**: 월요일 09:00 KST 첫 tick 정상 점화 확인 필요 (안 돌면 regression).

---

## 5. `../.ai/sessions/session-2026-04-18-0005.md` — 백테스트 persistence + Scout 실전 배선 + Real 전환

**시점**: 2026-04-18 오후 ~ 저녁 (복구 세션 종료 직후)  
**팀**: lead + 6 teammate (bypass / feeders / adapter / backfill / llmstats / candidates-ui)  
**선행**: Recovery 세션의 수정된 운영 상태  
**운영 영향**: ★★ — v2 잔존 12 컨테이너 완전 퇴역 + **paper → real 전환** + stop 이중 차단

### 5.1 백테스트 Persistence (migration 012)
v3 는 Python 코드 생성 모델이라 재현 단위가 "종목 리스트 25개" (v2) 가 아니라 **`context_snapshot + screening_candidates + code_text`**. scout_runs.context_snapshot_json JSONB + screening_candidates 테이블 신설 (rank / strategy / entry_hint / exit_hint / factors / promoted_to_sheet_id / rejection_reason 6종). StrategyEngine.build_sheet_with_reason 으로 거부 사유 코드화.

### 5.2 Scout 실전 모드 배선 (4 track 병렬)
첫 smoke 에서 "LLM 은 진짜 돌지만 input/executor 둘 다 stub" 발견. 트랙 4개로 동시 해소:
- **bypass**: `MACRO_AUTO_OVERRIDE_DISABLED` env
- **backfill**: KIS 60일 universe daily_prices 초기 적재 (4590 rows / 153 tickers)
- **feeders**: `slow_loop/scout/feeders/real.py` (Universe/News/Sector/Market)
- **adapter**: ScreeningToolAdapter wire + market_data DataFrame round-trip

Scout prompt 교정 (MultiIndex 구조 예시 + `.xs()` / `.unstack()` 안전 템플릿 + 안티패턴 3종) — 교정 후 같은 input 으로 candidates **0 → 10건** 실측.

### 5.3 운영 UI 보강 (2 track 병렬)
- **llmstats-write**: `infra/llm_stats.record_llm_call` 신규 + scout/macro/macro_shadow 각각 Redis HINCRBY. 이전엔 read 측만 있고 write 가 비어있어 UI 상단 이력 항상 공백이었음
- **candidates-ui**: `GET /scout/runs/{id}/candidates` + Scout Run Detail 하단 테이블 (rank/ticker/strategy/conviction/status/notes). 이후 종목명 `stock_masters LEFT JOIN` 추가

### 5.4 Logs 정렬 버그 — 03a7cd8
Loki 가 label set 별 여러 stream 반환 (kis-gateway 는 stdout/stderr 분리). 순차 concat 만 해서 시각 순서 섞임. timestamp 기준 전역 desc 정렬 + limit 을 병합 후 적용.

### 5.5 v2 잔존 12 컨테이너 완전 퇴역
Phase 2.10 에서 22→17 로 줄였다고 봤지만 실은 `prime-jennie-*` (non-runtime) 14개가 여전히 가동 중이었음. control-ui port 80 충돌 추적 중 발견. price-monitor/buy-scanner/scout-job/sell-executor/buy-executor + airflow 2 + kis-gateway/dashboard/dashboard-frontend + news-pipeline/telegram/job-worker = **12개 제거**. 남은 v2 3개 = vllm-llm/vllm-embed/qdrant.

### 5.6 Paper → Real 전환 (세션 말미)
사용자 "mock 모드 한계구나, stop 걸고 real 로 전환하자". KIS paper 2/sec rate limit 이 monitor balance polling 과 충돌하던 것이 계기.

실행 순서 (stop 먼저):
1. Redis `trading_flags:stop=1` + `control.state:stop` SET
2. `.env` 5줄 교체 (APP_KEY/SECRET/ACCOUNT_NO/BASE_URL/IS_PAPER — paper→real, 계좌 50156036→68211289, openapivts:29443→openapi:9443)
3. Paper token 백업 + 제거 (강제 재인증)
4. kis-gateway 재기동 → real OAuth 신규 발급
5. `/api/balance` smoke — 실계좌 응답 정상 (3 포지션: 현대차/고려아연/HD현대, 현금 341,498원, 총자산 200,552,998원)

**이중 차단 검증** (손절 방지):
- Redis `trading_flags:stop=1` — fast-loop `BalanceAwareSizer.__call__` 에서 `entry_allowed=False → qty=0` → entry skip
- v2 에서 산 3 포지션에 대한 v3 `position_sheets` = 0 rows → fast-loop `sheet_fetcher()` 빈 리스트 → exit 평가 대상 없음
- Stream ACK-first (`redis_streams.py:157`) → pending 에 안 남음. stop 해제 시 "우다다다" 폭주 불가
- `PositionSheet.valid_until` Pydantic validator → 장외 생성 시트 자체 거부
- `StrategyEngine.duplicate_today` → 같은 날 같은 ticker 중복 차단

### 5.7 MACRO_AUTO_OVERRIDE_DISABLED 영구 주입
real 전환 시 KOSPI 20d vol 58% 상태에서 auto_override 가 계속 closed → Scout 못 돌아 데이터 축적 0. stop 이중 차단 기간엔 데이터 축적 우선이라 `docker-compose.yml` slow-loop env 에 영구 추가 (`${…:-1}`). **실 매매 재개 전 반드시 제거 또는 "0"**.

**후속 (Phase 2.15 후보)**: Scout prompt few-shot / `SCOUT_VALID_UNTIL_BYPASS` / engine_error 세분화 / **RUNBOOK_REAL_MODE.md** (별도 문서 요구) / MACRO_AUTO_OVERRIDE 제거 시 알림 / 매수 신호 Telegram / worktree isolation 실검증.

---

## 6. `../.ai/sessions/session-2026-04-18-0006.md` — Logs 가시성 복구 + news 상시화 + vLLM FP8

**시점**: 2026-04-18 저녁 ~ 밤 (real 전환 세션 종료 직후)  
**팀**: lead 단독  
**선행**: Real 전환 직후 운영 상태  
**운영 영향**: Logs UI 정상화 + 월요일 첫 scout tick 전 news 상시 구동 + vLLM EXAONE KV 메모리 75% 절감

이슈 맵 (3건):
1. 모든 v3 데몬이 Loki 에 최근 5분 `NO STREAM` (user 는 news-pipeline 만 신고했지만 실은 전역) — **promtail 이 09:41 UTC 이후 2시간 완전 정지**. v2 잔존 `prime-jennie-vllm-*` / `qdrant-*` 컨테이너 로그 파일의 3월 말부터 누적 old timestamp 라인이 Loki `reject_old_samples_max_age` 를 유발 → 400 → **같은 batch 의 v3 신규 로그까지 전량 drop** → retry/backoff 루프
2. news-pipeline 장외/주말 idle — Phase 2.9 slice2 의 v2 3-스레드 상시 구조가 apscheduler `*/10 9-15 * * 1-5` 로 잘못 축소. EXAONE 로컬이라 비용 無인데 장중 한정 회귀
3. vLLM EXAONE KV cache fp16 — "vLLM 메모리 최적화 계획" 미적용. 동시 요청 / context 확장 제약

핵심 수정:
- **promtail**: `relabel_configs` 첫 줄에 `keep` action — `__meta_docker_container_label_com_docker_compose_project == 'prime-jennie-runtime'` 만 scrape. v2 공유 인프라 로그는 v3 UI 대상 아님 (`docker logs` / v2 grafana)
- **news_pipeline.crawl_cycle** cron: `*/10 9-15 * * 1-5` → `*/10 * * * *` (24/7 10분 주기). DB live UPDATE + Redis `scheduler.reload:news_pipeline` publish 로 즉시 반영
- **vLLM EXAONE**: `--kv-cache-dtype fp8_e4m3 --max-num-seqs 128`. 초기 시도는 FP8 단독 → sampler warmup (기본 256 dummy) peak 이 0.85 util 한계 넘어 OOM → `--max-num-seqs 128` 동반. KV cache 20,368 tokens, 4.96× concurrency

**후속**: 월요일 09:30 첫 scout tick 관측 (scout_runs + screening_candidates + position_sheets 생성 + Logs UI 4 탭 표시), FP8 KV 영향 모니터 (news_sentiments.score 분포), Phase 2 TurboQuant 준비 (nightly 이미지 + Korean eval harness).

---

## 7. `../.ai/sessions/session-2026-04-18-0007.md` — promtail 재회귀 재수정 + 배포 파이프라인 + 패밀리 docs + systemd

**시점**: 2026-04-18 22:00 ~ 자정 경 (logs_and_vllm_fp8 세션 42분 후)  
**팀**: lead 단독  
**선행**: 6번 세션의 promtail fix 주장  
**운영 영향**: promtail drop 46/s → 0, push-to-main 자동 배포, 패밀리 4 리포 문서 drift 정리, 재부팅 후 runner 자동 기동

핵심:
- 6번 세션의 promtail `keep` relabel 이 promtail 3.3.2 + docker_sd 환경에서 실제로는 작동 안 함. Loki 400 응답은 `"at least one label pair is required per stream"` (handoff 6 의 `reject_old_samples` 진단은 오류) → `docker_sd_configs.filters` (Docker API 레벨) 로 교체, relabel keep 는 `^...$` 로 안전망 유지
- 신규 GitHub Actions self-hosted runner `~/actions-runner-v3` 등록 + `.github/workflows/deploy.yml` — push to main → MS-01 에서 `git pull --ff-only` + `docker compose --profile full up -d --build`. `--profile full` 누락 시 16 서비스 중 postgres/redis 2개만 touch
- v2 legacy 12 컨테이너 재발견 (real_mode 세션에서 퇴역했지만 재부팅 or 수동 up 으로 부활). 다시 `docker update --restart=no + stop + rm` 로 일괄 제거
- 패밀리 4 리포 (runtime/control-ui/minyoung-mah, v2 제외) docs 6건 신규/재작성
- Runner systemd 영구화 (`sudo ./svc.sh install youngs75 && sudo ./svc.sh start`). docker-compose restart policy `unless-stopped` 이 16 서비스에 적용되어 runtime / control-ui 용 별도 systemd unit 불필요

커밋: `8be4786` (promtail) + `7e4ac44` (workflow) + `f43015b` (profile) + `ddaeb3a` (docs 4종) + `4cf1c9d` (이 핸드오프). control-ui `3ca32cd`, minyoung-mah `167fb1f`.

---

## 핸드오프 간 의존 관계 그래프

```
[0] 직전 상태
    │
    ▼
[1] 2026-04-18 Phase 2.10 — Agent Teams, v2→v3 포팅
    │
    ├─▶ [2] p2.11-2.12 — Control UI 5 page + Macro Shadow
    │       │
    │       ▼
    ├─▶ [3] p2.13 — news pipeline global + heartbeat + cloudflared
    │       │
    │       ▼
    ├─▶ [4] recovery — UI 공백 + apscheduler dow 함정
    │       │
    │       ▼
    └─▶ [5] real_mode — 백테스트 persistence + Scout 실전 + Real 전환 + v2 12 퇴역
            │
            ▼
        [6] logs_and_vllm_fp8 — promtail + news 24/7 + FP8 KV
            │
            ▼
        [7] (미문서화) promtail 재회귀 재수정 + GitHub Actions 배포 파이프라인
```

**주의**: 전부 "2026-04-18" 이라는 같은 날이다. 시작은 2026-04-17 밤. 집중 세션 시리즈 (약 24시간 내 6 handoff + 1 미문서화 = 7 단위 작업).

## 주제별 교차 참조

**실매매 관련**:
- 전환 절차 → 5.6
- MACRO_AUTO_OVERRIDE_DISABLED → 5.7, 6 의 news cron
- stop 이중 차단 → 5.6 5개 layer
- 롤백 → 5 의 "Real → Paper 복귀"

**스케줄러**:
- cron 표준 vs apscheduler 규약 → 4 번 이슈 6
- news_pipeline 24/7 확장 → 6
- global_news_crawl / digest → 3

**관찰성 (Logs / Metrics / Health)**:
- 데몬 heartbeat → 3 (2.13-5)
- cloudflared metrics → 3 (2.13-6)
- Loki 쿼리 라벨 → 3 (Ad-hoc)
- Loki nanosecond timestamp → 4 (이슈 3)
- Loki stream 병합 정렬 → 5.4
- promtail keep 필터 → 6, 7 (2번 수정)

**Control UI**:
- 4 page (Portfolio/Trades/Jobs/System) → 1
- 5 신규 page (Overview/Macro/Scout/LLMStats/Logs) → 2
- nginx DNS 영구 캐시 → 4 (이슈 1)
- Scout candidates + 종목명 → 5.3
- LLM Features 표 drift → 2 (addendum)

**v2 공존/퇴역**:
- 초기 퇴역 22→17 → 1
- 공유 인프라 경계 (vllm/qdrant/mariadb) → 1
- v2 bridge 전환 + runtime_shared 네트워크 → 4 (이슈 4-5)
- 잔존 12개 완전 퇴역 → 5.5
- v2 log 간섭 차단 (promtail) → 6, 7

**minyoung-mah (upstream)**:
- provider usage 전파 (v0.1.2) → 3 (2.13-3)
- PyPI publish 대기 상태 → 3, 6

## 다음 세션 시작 시 필수 체크

```bash
# 1. MS-01 v3 상태 (16 v3 서비스 + v2 공유 3)
ssh prime-jennie 'docker ps --format "table {{.Names}}\t{{.Status}}"'

# 2. 최신 main
cd ~/projects/prime-jennie-runtime && git log --oneline -8

# 3. 배포 파이프라인 상태
gh run list --repo youngs7596/prime-jennie-runtime --limit 3

# 4. 실매매 stop 여부 (해제됐으면 MACRO_AUTO_OVERRIDE_DISABLED 도 같이 해제됐는지 확인)
ssh prime-jennie 'docker exec prime-jennie-runtime-redis-1 redis-cli -a $REDIS_PASSWORD GET trading_flags:stop'
```
