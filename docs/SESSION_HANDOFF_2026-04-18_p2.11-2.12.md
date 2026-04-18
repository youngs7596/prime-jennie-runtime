# 세션 핸드오프 — 2026-04-18 (Phase 2.11 + 2.12)

**세션 범위**: Phase 2.11 (Control UI 보강) + Phase 2.12 (Macro feeder 실배선 + DeepSeek shadow)
**시작**: 2026-04-18 아침 (이전 Phase 2.10 세션 종료 직후) · **마감**: 같은 날 주간
**팀**: lead 단독 (Opus 4.7 1M)

## 결과 요약

- **Phase 2.11 Control UI 보강** — dashboard backend `/api/scout` 신설 + 5 신규 페이지 (Overview / Macro / Scout / LLM Stats / Logs) + Macro/Overview 풍부화
- **Phase 2.12 Macro feeder 실배선** — stub 3종 → 실데이터 (B1~B4)
- **Phase 2.12 Macro Shadow** — DeepSeek 을 Opus 와 병렬 평가, 결과 `macro_runs.metadata_json.shadow` 저장, UI 비교 섹션 (B5~B7)
- **부수 정리** — MS-01 exited v2 컨테이너 19개 정리, pj-track-{a..e} worktree 5개 정리, public repo PR 차단(interaction-limit `collaborators_only`, 만료 2026-10-17), Dockerfile 10개 `infra/docker/` 이동, 루트 dead `Dockerfile` 삭제

## Phase 2.11 — Control UI 보강

### Backend (prime-jennie-runtime)
- **`/api/scout` router 신설** (`prime_jennie_runtime/dashboard/routers/scout.py`)
  - `GET /scout/runs?limit=20` — 최근 runs (code_text 제외)
  - `GET /scout/runs/{id}` — 단일 run 상세 (code_text 포함)
  - `GET /scout/dates?limit=30` — 실행된 날짜 목록
  - `GET /scout/latest` — 최신 요약
  - 3 테스트 PASS
- **macro_runs / scout_runs persistence 배선** (`prime_jennie_runtime/slow_loop/persistence.py` 신규)
  - 기존: smoke/cron 이 LLM 을 돌려도 DB 에 기록 안 됨 (호출은 있었으나 persist 경로 미연결)
  - 수정: `council_logging.save_council_run` 재사용 + scout 단순 INSERT
  - `SlowLoopComponents.db_engine` optional 필드 추가
- **slow-loop env** — docker-compose.yml 에 `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `*_MODEL` / `*_BASE_URL` pass-through 5줄 추가. 키는 v2 `.env` 에서 복사
- **Dockerfile 재배치** — 10개 `Dockerfile.*` 를 `infra/docker/` 아래로 git mv, 루트 dead `Dockerfile` 삭제, compose `dockerfile:` 경로 10줄 업데이트

### Frontend (prime-jennie-control-ui @ youngs7596)
- 기존 4 page (Portfolio / Trades / Jobs / System) → **9 page 로 확장**
- 신규: **Overview** (5 KPI 카드 + Macro Timeline 바 차트 + Recent Scout + Recent Trades), **Macro** (리스트 + 상세), **Scout** (리스트 + code_text 뷰어), **LLM Stats** (일간/월간 + feature routing), **Logs** (Loki 프록시)
- `api.ts` v2 legacy MacroInsight/RegimeResponse 타입을 v3 MacroRun/gate+size_multiplier 스키마로 교체 (기존 페이지 미사용이라 영향 0)
- **Macro Detail 풍부화**: Top Risks 카드화 (severity 색상), Council Log Steps, 이전 run diff 배지, next_review_hint 강조, collapsible raw metadata, 상대시간 표시
- **Overview 풍부화**: 4 카드 → 5 카드 (+ Portfolio), Macro Timeline (최근 10 runs gate×size 바), Recent Scout 5건, Recent Trades 5건 + 실현 P&L

## Phase 2.12 — Macro feeder 실배선 + Shadow

### B1~B3: Real Feeder 3종 (`prime_jennie_runtime/slow_loop/macro/feeders/real.py` 신규)

| Feeder | 데이터 소스 |
|---|---|
| `RealMarketSnapshotFeeder` | Redis `macro:data:snapshot:{date}` (KOSPI/KOSDAQ/VIX/USD-KRW) + `us_market_daily` (SP500/NASDAQ) + `index_daily_prices` (KOSPI 20d/60d 연율화 변동성) + `daily_prices × stock_masters` (섹터 drops -2% 이하) |
| `RealKorMacroNewsFeeder` | `news_articles × news_sentiments`, 최근 24h abs(score) 상위 10건 |
| `RealWsjDigestFeeder` | `us_market_daily` 최신 + Redis snapshot VIX/regime 기반 factual digest (digest_id=SHA-256). **WSJ 실 본문 크롤러는 후속** |

미수집 필드 (Nikkei/HSI/USD-JPY/crude/gold/VKOSPI) 는 neutral placeholder — KOSPI/KOSDAQ 기반 closed 조건 판정에 치명적 영향 없음. numpy 없이 `statistics.stdev` 사용 (slow_loop 이미지 경량 유지).

### B4: 스위칭

`_build_slow_loop_components(redis_client, db_engine=...)` — DB engine 주입 시 real feeder, 아니면 stub fallback (기존 테스트 119개 영향 0).

### B5: 모델명/비용 추정

upstream minyoung-mah Orchestrator 가 실제 `usage.input_tokens` 를 `RoleInvocationResult.metadata` 에 싣지 않아, prompt_chars + output_chars 로 거칠게 추정 (±20%). `_PRICING` 표 (Opus 15/75, DeepSeek 0.27/1.10, …). `_tier_model(tier)` 로 env → 모델명 매핑.

### B6: Macro Shadow (DeepSeek)

- `_try_build_tiered_router` 반환에 `"shadow_reasoning": DeepSeek` 추가
- `_build_slow_loop_components` 가 `shadow_orchestrator` 생성 (reasoning tier 를 DeepSeek 로)
- `MACRO_SHADOW_ENABLED=0` env 로 비활성화 가능
- `pipeline.run_slow_loop`: `asyncio.gather` 로 primary + shadow 동시 실행
- shadow 결과 → `macro_runs.metadata_json.shadow` 에 `{model_used, gate, size_multiplier, reasoning, confidence, top_risks, latency_ms, cost_usd_estimated, next_review_hint}` 병합
- shadow 실패 시 primary 만 진행 (`{"error": "..."}` 기록)

### B7: UI Shadow 비교

Macro Detail 에 Primary (Opus) vs Shadow (DeepSeek) side-by-side 카드. gate match/diverge 배지, cost ratio 배지 (예: 62×), 양쪽 reasoning 6줄 clamp.

### 실측 관찰 (smoke)

| 항목 | Primary Opus | Shadow DeepSeek |
|---|---|---|
| gate | closed (post-proc high_volatility override) | open |
| size_multiplier | 0 | 0.75 |
| confidence | high | medium |
| LLM 원본 판단 | open | open |
| cost 추정 | **$0.0408** | **$0.00065** |
| latency | 9.6s | 13.4s |

**비용 62배 차이**. 둘 다 LLM 은 open 판단 → post-processor 가 Primary 만 high_volatility auto-override. KOSPI 20d vol 58% (실제 값, 최근 ±2% 급변 누적).

## 커밋 목록 (main)

```
446107f fix(slow_loop): shadow state key 는 step name(macro_gate)
d61cc18 feat(slow_loop): Macro shadow (DeepSeek) + cost/model 추정 — Phase 2.12 B5/B6
fae2ac4 fix(slow_loop): daily_prices.trade_date → price_date (B1 feeder 컬럼명 수정)
56b4a46 feat(slow_loop): Macro feeder 3종 실구현 — B1/B2/B3 (Phase 2.12)
d0cadc9 feat(slow_loop): macro_runs / scout_runs persistence 배선
45c856d fix(compose): slow-loop 에 LLM 키 pass-through — Macro/Scout 활성 조건
8ed49bd feat(dashboard): scout router — scout_runs 이력 조회 API 4종
dfa9263 refactor(docker): Dockerfile 10개 infra/docker/ 로 이동 + dead 루트 Dockerfile 제거
```

control-ui 커밋:
```
2987076 feat: Macro Detail 에 Shadow (DeepSeek) 비교 섹션
500e216 feat: Macro Detail + Overview 풍부화
a462756 feat: Phase 2.11 pages — Overview / Macro / Scout / LLMStats / Logs
```

## 운영 현황

- **MS-01 16 컨테이너 Up** — 이번 세션에 dashboard + slow-loop + control-ui 재기동 (새 코드 반영)
- **운영 모드**: paper 유지 (실매매 0)
- **Macro/Scout**: DB persistence 배선 완료, LLM 키 pass-through 후 real feeder 로 동작. 매 평일 08:30~14:30 KST 30분 마다 7회 자동 돌며 macro_runs + macro_runs.metadata_json.shadow 에 데이터 누적
- **Cron 기반 자동 shadow 수집 시작**: 다음 개장일(2026-04-21 월) 부터 매일 14 run (7 primary + 7 shadow) × 영업일 누적. 2-3주 후 Macro 모델 스위치 결정 근거 충분

## 결정 이력

1. **Public repo 유지 + collaborators_only interaction-limit** (2026-04-18)
   - 이유: GitHub Actions 쿼터가 public 에서 더 크기 때문
   - 만료 **2026-10-17** — 갱신 재설정 필요. `project_interaction_limit.md` 메모리 기록
2. **Macro 를 Opus 로 유지 → Shadow 비교 인프라 우선 구축** (2026-04-18)
   - 데이터 누적 후 스위치 결정 (DeepSeek 62× 저렴, 판단 품질은 측정 필요)
3. **WSJ 실 크롤러 후순위** (Phase 2.13 후보)
   - 현재 us_market_daily factual digest 로 대체
4. **Feeder stub 잔여 필드 (Nikkei/HSI/USD-JPY/crude/gold)** → 별도 데이터 수집 job 작성 필요 (후속)

## Phase 2.13 후보

- **WSJ/Bloomberg/Reuters 실 크롤러** — `RealWsjDigestFeeder` 본문 요약 (LLM 요약 단계 추가)
- **Nikkei/HSI/USD-JPY/crude/gold 수집 job** — 현재 placeholder. macro_collect_global 확장 or 별도 핸들러
- **upstream orchestrator patch** — minyoung-mah RoleInvocationResult.metadata 에 실제 Anthropic/OpenAI usage 싣기 (추정 대신 실측 비용)
- **Macro 모델 스위치 결정** — 2-3주 shadow 데이터 누적 후 Opus→DeepSeek 혹은 DeepSeek R1 평가
- **daemon application-level heartbeat** — container state 보다 세밀 (redis key)
- **cloudflared metrics probe (`:2000/metrics`) smoke 완화**
- **real mode 전환 체크리스트 실행**
- **control-ui 페이지 추가 보강** — Scout Detail 에 factor_weights 바 차트, LLM Stats 에 일별 스택 bar (추가 API 필요)

## 참고 명령

- slow-loop 재기동 (env 변경 후): `docker compose --profile full up -d --force-recreate slow-loop`
- smoke 1회: MS-01 에서 `docker cp scripts slow-loop:/tmp/ && docker exec slow-loop sh -c "cd /tmp && python -m scripts.smoke_slow_loop_once"`
- macro/shadow 확인: `docker exec postgres psql ... -c "SELECT macro_run_id, gate, cost_usd, jsonb_pretty(metadata_json->'shadow') FROM macro_runs ORDER BY generated_at DESC LIMIT 1"`
- UI: Cloudflared 터널 URL 또는 `http://192.168.31.195/` (내부)

---

## Addendum — LLM Features / Shadow 모델 정정 (같은 날 저녁)

사용자가 LLM Stats 페이지 Features 표의 엉터리 매핑 발견 후 연쇄 수정.

### 1. `LLMConfig` drift — Features 표가 실제 라우팅과 어긋남

- `infra/config.LLMConfig` 가 `env_prefix="LITELLM_MODEL_"` 로 v2 LiteLLM 시절 설정. 기본값이 `reasoning: deepseek/deepseek-reasoner`, `fast: ollama/exaone3.5:32b` 로 고정
- 하지만 v3 slow_loop/briefing 은 langchain-openai/anthropic 직접 호출이라 `ANTHROPIC_MODEL` / `DEEPSEEK_MODEL` env 사용 → UI 가 Macro/Briefing 을 deepseek-reasoner 로 잘못 표시
- **수정**: `dashboard/routers/llm_stats._service_model(service, cfg)` 가 service 별 실제 env 를 source-of-truth 로 해석. provider 라벨 (vLLM / DeepSeek / Anthropic) 추가. macro_shadow feature 도 테이블에 노출
- **news_analysis 기본값**: `ollama/exaone3.5:32b` → `LGAI-EXAONE/EXAONE-4.0-32B-AWQ` (compose 기본값과 동기)
- frequency 문구 현실 반영: "장 시작 전" / "매 시간" → "평일 08:30~14:30 매시 30분 (7회/일)"

### 2. DeepSeek 모델 identifier 계보 정정

당초 "reasoner = reasoning tier" 로 단순 매칭해서 shadow 를 `deepseek-reasoner` 로 전환했으나, DeepSeek API 의 identifier 의미가 달랐음:

| Identifier | 실제 매핑 | Opus 대응 여부 |
|-----------|----------|-----------------|
| `deepseek-chat` | **V3.2 (최신 flagship, 하이브리드 thinking)** | **✓ (apples-to-apples)** |
| `deepseek-reasoner` | R1 (구세대 reasoning 전용) | 스키마 literal 위반 관찰 (`category="volatility"` 허용값 밖) |
| `deepseek-v3.1` / `v3` | 구버전 (하위 호환) | - |

`-chat` 이 항상 최신 flagship 을 가리키는 alias 라 이미 V3.2 로 연결되고 있었음. 처음 `strong` tier 를 shadow 에 재사용했던 구성이 결과적으로 정답이었음.

### 3. DeepSeek reasoner 도입 시도 & 롤백

- `_DeepSeekReasonerOpenAI` wrapper 추가 (tool_choice 미지원 → `method="json_mode"` 기본). wrapper 자체는 향후 override 대비 유지
- `DEEPSEEK_SHADOW_MODEL` env 신설 (기본 `deepseek-chat`). `=deepseek-reasoner` 로 override 하면 json_mode + reasoner wrapper 자동 선택
- compose 에 env pass-through

### 최종 Shadow 구성 (smoke 재검증)

- Primary: `claude-opus-4-7` (Anthropic) — open/0.75 판단 → post-processor high_volatility override 로 closed
- Shadow: `deepseek-chat` = **V3.2 flagship** — open/0.75 판단, latency/cost 훨씬 저렴
- DB 저장 확인: `metadata_json->'shadow'` 에 model_used=deepseek-chat, gate=open, size_multiplier=0.75

### 추가 커밋

```
d752b96 fix(slow_loop): Shadow default 를 deepseek-reasoner → deepseek-chat (V3.2 flagship)
bd6ad33 fix(slow_loop): DeepSeek reasoner(R1) 는 tool_choice 미지원 → json_mode
566cc3d feat(slow_loop): Macro Shadow 를 DeepSeek chat → reasoner (R1) 로 전환 (후속 롤백)
d83c246 fix(dashboard): LLM Features model/provider 실제 env 기반 해석
```
