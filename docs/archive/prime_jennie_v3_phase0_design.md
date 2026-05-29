# Prime Jennie v3 — Phase 0 설계 문서

> **문서 목적**: Claude Code 및 병렬 작업자가 이 문서 하나만 읽고 v3 구축에 착수할 수 있도록 한다.
>
> **작성자**: 민지 (Claude) × 영석
> **작성일**: 2026-04-16
> **버전**: **0.3** (세부 스펙 3종 작성 완료 + 피드백 2차 반영)

---

## CHANGELOG

### v0.3 (2026-04-16)
세부 스펙 3종(`POSITION_SHEET_SPEC.md`, `SCOUT_CODE_GENERATION.md`, `MACRO_GATE_SPEC.md`) 작성 완료. Claude Code 2차 리뷰 피드백 반영.

**메인 문서 변경**:
- **§5.1 exit rules 표 확장**: 7종 → **9종**. `profit_floor`, `death_cross` 추가. v2 12-rule 커버리지 완성.
- **§5.2 outcomes.exit_reason comment 갱신**: 새 exit_reason 값 6개 추가 (profit_floor, death_cross, overextension, unfilled 등).

**세부 스펙 문서들**:
- `POSITION_SHEET_SPEC.md` v0.2 — profit_floor/death_cross 신설, 권장 순서 8개 rule 재배치, 테스트 6건 추가
- `SCOUT_CODE_GENERATION.md` v0.2 — `consensus_data` DataFrame 별도 제공 명시, 예제 코드 4팩터 결합으로 갱신
- `MACRO_GATE_SPEC.md` v0.2 — 이산화 half-open interval 정의, open+0.0 모순 처리 신설, 테스트 8건 추가

### v0.2 (2026-04-16)
Claude Code v2 컨텍스트 리뷰 피드백 전건 반영 + Control UI 신설.

**주요 변경**:
1. repo 구조 3개 → **4개** (`prime-jennie-control-ui` 신설)
2. 컨테이너 10개 → **13개**
3. `kis-gateway` 별도 컨테이너로 **복원** (v2 포팅)
4. `news-pipeline-kor` 신설 (v2 포팅)
5. `telegram-bot` in-scope로 **복원**, 긴급 제어 도구
6. **§4.8 Control UI 신설** (Next.js 15 + Cloudflare Access)
7. 포지션 시트 `exit.rules[]` 확장 (breakeven, scale_out 포함)
8. Performance DB 스키마 owner=runtime, meta/UI=read-only 명시
9. Screening Executor 화이트리스트에 sklearn submodule 추가
10. Phase 1 Track E 신설 (v2 데이터 마이그레이션)
11. Open Q #6 closed: **$50 soft / $100 hard**
12. Non-goals에서 "UI 안 만든다" 삭제
13. §13 신설: v2 → v3 데이터 마이그레이션 전략

### v0.1 (2026-04-16)
초안.

---

## 0. Executive Summary

Prime Jennie v3는 **네 개의 독립 리포**로 구성된 자가진화 트레이딩 시스템이다.

- **minyoung-mah** (기존, 라이브러리)
- **prime-jennie-runtime** (신규): 실행 엔진
- **prime-jennie-meta** (신규): 자가진화 엔진
- **prime-jennie-control-ui** (신규): 영석 전용 모니터링/제어 UI

| 축 | v2 | v3 |
|---|---|---|
| LLM 사용 위치 | 파이프라인 전반 | 느린 루프에만 |
| Scout 출력 | 자연어 스코어 | **스크리닝 코드(Python)** |
| 매크로 역할 | 자연어 어드바이저 | **바이너리 게이트 + 사이즈 배수** |
| 합의 메커니즘 | Bull/Bear/Judge | 단일 LLM structured output |
| Eval & 진화 | 수동 | Harness 기반 자동 PR |
| 컨테이너 수 | 31개 | **13개** (v2의 42%) |
| 메시지 브로커 | RabbitMQ | Redis Stream |
| 운영 제어 | Telegram 전용 | **Telegram + Control UI** |

---

## 1. 설계 원칙

### 1.1 LLM 배치 원칙
LLM은 **비정형 해석 · 패턴 인식 · 코드 생성**에만. 실시간·일관성·속도는 결정론적 코드.

### 1.2 재귀 루프 원천 차단
meta는 runtime만 수정. 자기 자신과 minyoung-mah는 GitHub token scope로 **물리적 차단**.

### 1.3 Harness 오염 금지
Prime Jennie 도메인 특화 코드는 minyoung-mah에 들어가지 않는다.

### 1.4 Provenance First
모든 매매 결정·코드 변경에 출처와 생성 컨텍스트가 따라붙는다.

### 1.5 Fail Loud, Not Silent
`try/except: pass` 금지. 모든 실패는 observer event.

### 1.6 v2 자산 최대 활용 (신규)
v2에서 검증된 서비스(KIS Gateway, News Pipeline, Telegram 제어)는 **그대로 포팅**. 재작성 금지. v2가 안정적이었던 부분을 다시 짜는 건 자존심 낭비.

---

## 2. 전체 아키텍처

### 2.1 Repo 구조 (4개)

```
youngs75/
├── minyoung-mah/                     [라이브러리, 0.1.0+]
│
├── prime-jennie-runtime/             [실행 엔진]
│   ├── 느린 루프: Scout, Macro Gate, Strategy
│   ├── 빠른 루프: Executor
│   ├── News Pipeline KOR
│   ├── Screening Executor
│   ├── 백테스트 엔진
│   └── (v2 포팅) KIS Gateway, Telegram Bot
│
├── prime-jennie-meta/                [self-evolving 엔진]
│   ├── Eval Pipeline
│   ├── PR Generator
│   └── runtime repo에만 write 권한
│
└── prime-jennie-control-ui/          [영석 전용 UI]
    ├── Next.js 15 (App Router, RSC)
    ├── Postgres/Redis/GitHub read
    └── Redis pub/sub으로 runtime에 명령
```

### 2.2 데이터 흐름

```
                    ┌─────────────────────────┐
                    │  News Pipeline KOR       │
                    │  네이버 → EXAONE → Qdrant │
                    └─────────┬───────────────┘
                              │ news vectors
                              v
┌────────────────────────────────────────────────────┐
│            prime-jennie-runtime                     │
│                                                     │
│  [느린 루프]                                        │
│   Scout ──> 스크리닝 코드                           │
│     │         │                                     │
│     │         v                                     │
│     │      Screening Executor (격리)                │
│     │         │                                     │
│     │         v                                     │
│     └──> Strategy Engine ──> 포지션 시트           │
│                     ^              │                │
│                     │              │                │
│                  Macro Gate        │                │
│                  (WSJ 입력)        │                │
│                                    v                │
│  ─────────────────────── Redis Stream ──────      │
│                                    │                │
│  [빠른 루프]                       v                │
│                               Executor              │
│                                    │                │
│                                    v                │
│                         KIS Gateway (v2 포팅)       │
│                                    │                │
│                                    v                │
│                             증권사 실주문           │
└──────────────────┬────────────────────────────────┘
                   │ (체결/성과)
                   v
            Performance DB (Postgres)
                   │
        ┌──────────┴──────────┐
        v                     v
┌────────────┐         ┌─────────────────┐
│   meta     │         │  Control UI     │
│  (read)    │         │  (read + 제어)   │
└────────────┘         │  Telegram Bot   │
                       │  (동일 명령 경로)│
                       └─────────────────┘
```

### 2.3 의존성 방향

```
runtime                 ──> minyoung-mah
meta                    ──> minyoung-mah
meta                    ──> runtime repo (PR만, write)
control-ui              ──> Postgres (read), Redis (read+pub), GitHub API (read)

runtime은 meta/ui/telegram의 존재를 모른다.
runtime은 Redis sub만 열어두고, 누가 sub하든 관심 없음.
```

---

## 3. minyoung-mah 계약

(v0.1 그대로. Control UI는 minyoung-mah를 직접 소비하지 않음.)

### 3.1 사용하는 프로토콜

| Protocol | runtime | meta |
|---|---|---|
| `SubAgentRole` | Scout, Macro, Strategy Reviewer | Eval Analyst, PR Generator |
| `ToolAdapter` | Screening, Backtest, KisGateway, News | GithubPR, PerformanceDB |
| `Orchestrator` | 느린 루프 파이프라인 | Eval → PR 파이프라인 |
| `ModelRouter` | FAST/DEFAULT/STRONG/REASONING | 동일 |
| `MemoryStore` | Scout 판단, 시장 히스토리 | PR 이력 |
| `HITLChannel` | Telegram 매매 긴급 | Telegram PR 승인 |

### 3.2 건드리지 않는 것
- Harness 내부 직접 import 금지
- Prime Jennie 전용 필드 요청 금지 (consumer extension으로 해결)

### 3.3 예상 gap feedback
- `StaticPipeline` 조건부 분기
- `ModelRouter` 비용 상한
- `Observer` latency budget

로컬 workaround 우선, 2회 이상 반복 시 Harness PR.

---

## 4. prime-jennie-runtime 상세 설계

### 4.1 디렉토리 구조

```
prime-jennie-runtime/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── docker-compose.yml
│
├── prime_jennie_runtime/
│   ├── slow_loop/
│   │   ├── scout/
│   │   ├── macro/
│   │   └── strategy/
│   │
│   ├── fast_loop/
│   │   ├── executor.py              # LLM 없음
│   │   ├── kis_client.py            # KIS Gateway HTTP 래퍼 (얇음)
│   │   ├── risk_throttle.py         # v2 포팅
│   │   └── overextension_filter.py  # v2 포팅
│   │
│   ├── kis_gateway/                 # v2 포팅, 별도 컨테이너
│   │   ├── server.py                # FastAPI
│   │   ├── token_manager.py         # 24h 갱신, 401/403 자동
│   │   ├── order_client.py          # 2-step 확인 (CCLD_DVSN 01→00)
│   │   ├── rate_limiter.py          # 19/sec 시세, 5/sec 매매
│   │   └── circuit_breaker.py       # fail_max=20, cooldown=60s
│   │
│   ├── news_pipeline_kor/           # v2 포팅
│   │   ├── crawler.py               # 네이버 금융 뉴스
│   │   ├── sentiment.py             # EXAONE 4.0 Q8
│   │   ├── embedder.py              # kure-v1
│   │   ├── qdrant_client.py
│   │   └── scout_feeder.py          # Scout용 news_score 제공
│   │
│   ├── telegram_bot/                # v2 포팅 + 확장
│   │   ├── bot.py
│   │   ├── commands.py              # /stop /pause /liquidate /dryrun /status
│   │   └── redis_publisher.py       # control.commands Stream
│   │
│   ├── screening_executor/
│   ├── backtest/
│   ├── position_sheet/
│   └── infra/
│       ├── redis_streams.py
│       ├── litellm_config.py        # Langfuse callback
│       └── observer_impl.py
│
├── data/
├── migrations/                       # v2 → v3
│   ├── 001_v3_schema.sql
│   ├── 002_legacy_namespace.sql
│   ├── migrate_signal_logs.py
│   ├── migrate_trade_logs.py
│   ├── migrate_daily_quant_scores.py
│   └── verify_migration.py
│
├── tests/
└── docs/
    ├── ARCHITECTURE.md
    ├── POSITION_SHEET_SPEC.md
    ├── SCOUT_CODE_GENERATION.md
    ├── MACRO_GATE_SPEC.md
    └── DB_SCHEMA.md
```

### 4.2 Scout — 스크리닝 코드 생성자

**책임**: 시장 상황 해석 → 매수 후보 **필터링 Python 코드 생성**.

**입력**:
- 최근 30거래일 OHLCV
- 섹터 모멘텀 팩터
- 전방 컨센서스
- **news_pipeline_kor의 news_score** (ticker별, staleness 포함)
- 직전 Scout 실행 결과 (memory)

**structured output**:
```python
class ScoutOutput(BaseModel):
    screening_code: str
    code_hash: str                   # sha256
    hypothesis: str
    expected_candidates: int
    factor_weights: dict[str, float]
    fallback_strategy: str
```

**생성 코드 계약**:
```python
def screen(market_data: pd.DataFrame, context: dict) -> list[ScreeningCandidate]:
    """
    market_data: 멀티인덱스 (ticker, date) DataFrame
    context: {
      "as_of": date,
      "universe": list[str],
      "news_scores": dict[ticker, float],      # -1.0 ~ +1.0
      "news_timestamps": dict[ticker, datetime],
      "sector_momentum": dict[sector, float],
      "consensus_estimates": dict[ticker, dict],
    }
    반환: ScreeningCandidate 리스트 (최대 20개)
    """
```

**모델**: tier `STRONG` (qwen3-coder-next)

**실행 위치**: Screening Executor 컨테이너 (§6.1)

### 4.3 Macro Gate

**책임**: 거시 환경 → **두 개 숫자**만 출력.

```python
class MacroGateOutput(BaseModel):
    gate: Literal["open", "closed"]
    size_multiplier: float           # 0.0 ~ 1.0
    # 부가 (로깅만, 실행 로직 참조 금지)
    reasoning: str
    top_risks: list[str]
    news_digest_ref: str
```

**원칙**:
- `gate == "closed"` → 그날 신규 포지션 0. 기존 포지션 청산은 Executor 룰.
- `reasoning` **절대 실행 로직 참조 금지**. 이거 안 지키면 게이트가 어드바이저로 퇴화.

**주기**: 매일 08:00 KST + ad-hoc 재실행 가능

**모델**: tier `REASONING` (qwen3-max)

### 4.4 Strategy Engine

**LLM 안 씀**. 결정론적 룰엔진.

```
for candidate in scout_candidates:
    if macro_gate.gate == "closed": skip
    
    base = strategy_policy.base_size(candidate.strategy_tag)
    macro_mult = macro_gate.size_multiplier
    risk_mult = risk_throttle.current()
    final = base * macro_mult * risk_mult
    
    if final < MIN_POSITION_PCT: skip
    
    sheet = build_position_sheet(...)  # exit.rules[] 포함
    redis.xadd("position_sheets", sheet)
```

### 4.5 Executor — 빠른 루프

**절대 원칙**: LLM 호출 금지.

**KIS 복잡도 분리**: Executor는 `kis_client.py`로 **얇은 HTTP 클라이언트**만. 실제 복잡도(토큰/rate limit/circuit breaker/2-step)는 **별도 kis-gateway 컨테이너**. v2 가장 안정적 서비스 포팅.

**포팅 컴포넌트**:
- KIS Gateway 서비스 전체
- Intraday Risk Throttle (5단계, min() 방식)
- Overextension Filter
- GAP_UP_REBOUND 전략

### 4.6 백테스트 엔진

- Grid Search 2,800개 조합
- **look-ahead bias 방지** 테스트 강제
- 슬리피지 모델: 거래량 대비
- `BacktestReport` → Performance DB

runtime 자체 실행 + meta PR 검증용. `BacktestToolAdapter`로 Harness 프로토콜에 얹음.

### 4.7 컨테이너 구성 (13개)

```yaml
services:
  slow-loop:           # Scout + Macro + Strategy
  fast-loop:           # Executor
  kis-gateway:         # v2 포팅
  screening-executor:  # 격리 샌드박스
  news-pipeline-kor:   # v2 포팅
  telegram-bot:        # v2 포팅 + 확장
  backtest-runner:
  redis:               # Stream + pub/sub
  postgres:            # Performance DB
  qdrant:              # news RAG
  vllm-exaone:         # EXAONE 4.0 Q8 + kure-v1
  monitoring:          # Prometheus + Grafana
  control-ui:          # Next.js 15
# 합계: 13개
```

### 4.8 Control UI (신설)

**리포**: `prime-jennie-control-ui` (별도)

**목적**: 영석 전용 모니터링 + 긴급 제어. Grafana로 대체 불가.

#### 4.8.1 기술 스택

| 계층 | 선택 | 근거 |
|---|---|---|
| 프레임워크 | Next.js 15 (App Router, RSC) | BFF 패턴, 별도 API 컨테이너 불필요 |
| 언어 | TypeScript | 스키마 공유 (pydantic → zod) |
| 스타일 | Tailwind CSS + shadcn/ui | 단일 사용자에 최적 |
| 차트 | Recharts | React 친화 |
| 아이콘 | Lucide | shadcn 표준 |
| 폰트 | Geist Sans + Geist Mono | 숫자 가독성 |
| 테마 | 다크 기본, 라이트 옵션 | 새벽 매크로 브리핑 |
| 실시간 | Server-Sent Events | WebSocket 오버엔지 회피 |
| 인증 | **Cloudflare Access + Google OAuth** | §4.8.4 |

#### 4.8.2 페이지 구성

```
/              대시보드 홈
  - 장 세션 상태, Macro Gate, Risk Throttle 레벨
  - 활성 포지션 요약, 오늘 PnL
  - 실시간 이벤트 스트림 (SSE)

/positions     포지션
  - 활성 + 히스토리 테이블
  - 드릴다운: provenance 패널 (Scout 코드, Macro 스냅샷)

/scout         Scout 랩
  - Scout run 목록
  - 스크리닝 코드 뷰어 (syntax highlight, diff)
  - 가설 + 팩터 가중치
  - 코드별 백테스트 결과

/macro         Macro 로그
  - Gate 이력 타임라인
  - reasoning + news_digest
  - size_multiplier 추이

/meta          Meta PR 센터 (Stage 0 핵심)
  - 승인 대기 PR 큐
  - 카드별 [리뷰] [승인] [Reject] [Revert]
  - Stage 현황 + 승격 진행률
  - Revoke 히스토리

/performance   성과
  - 누적 PnL 차트
  - 전략 태그별 breakdown
  - Sharpe, MDD, Hit Rate
  - v2 vs v3 비교 (Phase 2~)

/control       긴급 제어
  - [STOP ALL] 2-step 확인
  - [PAUSE]
  - [LIQUIDATE] 3-step 확인
  - [DRYRUN] 토글
  - 모든 명령 Telegram echo

/settings
  - LLM 비용 현황 (soft cap 진행률)
  - Stage 수동 조정 (0↔1만)
```

#### 4.8.3 데이터 접근 (BFF 패턴)

**읽기**:
```
Next.js Server Component / API Route
  ├── Postgres (read-only user pj_ui)
  ├── Redis (read + pub)
  └── GitHub API (read)
```

**쓰기 (명령)** — Redis pub/sub 단일 경로:
```
UI /control STOP 클릭
  → POST /api/control/stop
    → Redis PUBLISH control.commands {action, requester:"ui", ts}
      → Executor 구독 후 수행
      → Telegram Bot 구독 후 영석에게 echo
```

**동일 명령 경로** 원칙: UI와 Telegram 모두 동일 Redis 채널. Executor는 출처 불문 일관 처리. 긴급 제어 이중 구현 방지.

#### 4.8.4 인증 — Cloudflare Access (필수)

**배포**:
```
인터넷 → Cloudflare Edge → Cloudflare Access (Google OAuth)
  → Cloudflare Tunnel → MS-01 Control UI 컨테이너
```

**정책**:
- 이메일 allowlist: 영석 개인 Google 계정만
- 세션: 24시간
- 파괴적 액션(`/control/*`): **추가 one-time PIN**

**없으면 안 되는 이유**:
- 파괴적 액션(`/liquidate`, `/stop`) 존재
- URL 노출 경로 다수: 히스토리, 클립보드, 로그, 스크린샷, 와이파이 캡처
- Tunnel ≠ 인증. 혼동 금지.

**비용**: Access 50 user free. 영석 1명 영구 무료.

**영석 승인 대기 중**.

#### 4.8.5 디자인 방향

- Vercel / Linear / Raycast 계열 미니멀 다크
- glassmorphism 과용 금지
- 숫자는 monospace tabular-nums
- 시맨틱 컬러:
  - open / 긍정 → emerald-500
  - closed / 부정 → rose-500
  - shadow → amber-500
  - neutral → zinc-400
- 여백 넉넉, 정보 밀도보다 가독성
- **모바일 우선** (장중은 대부분 폰)

#### 4.8.6 UI 리포 디렉토리

```
prime-jennie-control-ui/
├── README.md
├── AGENTS.md
├── package.json
├── next.config.mjs
├── tsconfig.json
├── tailwind.config.ts
├── Dockerfile
│
├── src/
│   ├── app/
│   │   ├── (dashboard)/
│   │   │   ├── page.tsx
│   │   │   ├── positions/page.tsx
│   │   │   ├── scout/page.tsx
│   │   │   ├── macro/page.tsx
│   │   │   ├── meta/page.tsx
│   │   │   ├── performance/page.tsx
│   │   │   ├── control/page.tsx
│   │   │   └── settings/page.tsx
│   │   ├── api/
│   │   │   ├── stream/route.ts       # SSE
│   │   │   └── control/
│   │   │       ├── stop/route.ts
│   │   │       ├── pause/route.ts
│   │   │       └── liquidate/route.ts
│   │   └── layout.tsx
│   │
│   ├── components/
│   │   ├── ui/                       # shadcn/ui
│   │   ├── charts/
│   │   ├── position/
│   │   ├── provenance/
│   │   └── meta/
│   │
│   ├── lib/
│   │   ├── db.ts                     # Postgres read-only
│   │   ├── redis.ts
│   │   ├── github.ts
│   │   └── schema.ts                 # zod (pydantic 동기화)
│   │
│   └── styles/
│
└── tests/e2e/
```

### 4.9 News Pipeline KOR (신설)

**책임**: Scout의 `news_score` 공급.

**파이프라인**:
```
네이버 금융 뉴스 크롤러 (10분 주기)
  → 중복 제거
    → EXAONE 4.0 Q8 감성분석 (-1.0 ~ +1.0)
      → kure-v1 임베딩
        → Qdrant 저장

Scout 실행 시:
  ticker별 최근 N시간 감성 평균 → news_score
  + RAG로 관련 뉴스 top-k
```

**Latency 요구**: 크롤링→Qdrant 저장 5분 이내. Scout 최소 주기(1일) 대비 안전 마진 충분.

**v2 포팅**: 전체 코드 포팅, 감성분석 프롬프트만 재검증.

---

## 5. 인터페이스 사양

### 5.1 포지션 시트 JSON (schema v1.1)

```json
{
  "sheet_id": "ps_20260416_005930_a3f2",
  "schema_version": "1.1",
  "generated_at": "2026-04-16T09:15:00+09:00",
  "valid_until": "2026-04-16T15:20:00+09:00",

  "ticker": "005930",
  "side": "long",
  "strategy_tag": "GAP_UP_REBOUND",

  "size": {
    "base_pct": 0.05,
    "macro_multiplier": 0.7,
    "risk_multiplier": 1.0,
    "final_pct": 0.035,
    "max_notional_krw": 5000000
  },

  "entry": {
    "trigger": "limit",
    "price": 71200,
    "valid_until": "2026-04-16T10:30:00+09:00",
    "conditions": [
      {"type": "price_below", "value": 71500}
    ]
  },

  "exit": {
    "rules": [
      {"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03},
      {"type": "fixed_sl", "pct": 0.05},
      {"type": "breakeven", "activate_pct": 0.03, "floor_pct": 0.003},
      {"type": "scale_out", "levels": [[0.03, 0.25], [0.05, 0.25]]},
      {"type": "time_stop", "mode": "eod"}
    ],
    "priority": "first_match"
  },

  "provenance": {
    "scout_run_id": "scout_20260416_0900",
    "scout_code_hash": "sha256:abc123...",
    "scout_hypothesis": "반도체 섹터 모멘텀 재점화",
    "macro_state_snapshot": {
      "gate": "open",
      "size_multiplier": 0.7,
      "gate_run_id": "macro_20260416_0800"
    },
    "news_score_at_generation": 0.42,
    "strategy_policy_version": "v3.0.1",
    "generated_by": "prime-jennie-runtime@v3.0.1"
  }
}
```

**지원 `exit.rules[].type`**:

| type | 파라미터 | v2 검증 |
|---|---|---|
| `trailing_tp` | `activate_pct`, `drop_pct` | ✅ |
| `fixed_tp` | `pct` | ✅ |
| `fixed_sl` | `pct` | ✅ |
| `breakeven` | `activate_pct`, `floor_pct` | ✅ (+3%→+0.3% 바닥) |
| `scale_out` | `levels: [[pct, portion]]` | ✅ |
| `time_stop` | `mode: eod\|hold_days`, `value?` | ✅ |
| `overextension_exit` | `rsi_threshold` | ✅ |
| `profit_floor` | `activate_pct`, `floor_pct` | ✅ (+15%→+10% 바닥) |
| `death_cross` | `ma_short`, `ma_long`, `min_loss_pct` | ✅ (5/20 + -1% 이상 손실) |

v2의 12-rule 중 실전 검증된 **9종**. 나머지 3종은 통계 유의성 부족으로 제외. 세부 명세는 `POSITION_SHEET_SPEC.md` §5.2.

**불변식**:
- `size.final_pct == base * macro_mult * risk_mult` (부동소수 오차 허용)
- `valid_until > generated_at`
- `entry.valid_until <= valid_until`
- `provenance.scout_code_hash`는 실제 실행 코드 해시와 일치
- `exit.rules[]` 최소 1개, **`fixed_sl` 필수** (무한 손실 방지)
- `exit.priority == "first_match"`: 배열 순서대로 첫 매칭 적용

### 5.2 Performance DB 스키마 (소유권)

**owner**: `prime-jennie-runtime`. 마이그레이션 파일은 runtime repo에만.

**접근 권한** (Postgres role로 DB 레벨 차단):

| 역할 | user | 권한 |
|---|---|---|
| runtime | `pj_runtime` | read + write |
| meta | `pj_meta` | **read-only** |
| control-ui | `pj_ui` | **read-only** |
| 영석 debug | `pj_admin` | read + write |

**핵심 테이블**:

```sql
TABLE position_sheets
  sheet_id TEXT PRIMARY KEY,
  generated_at TIMESTAMPTZ,
  valid_until TIMESTAMPTZ,
  ticker TEXT, strategy_tag TEXT,
  sheet_json JSONB, provenance_json JSONB
  -- idx: generated_at, ticker, strategy_tag, (provenance_json->>'scout_run_id')

TABLE executions
  execution_id BIGSERIAL PK,
  sheet_id TEXT REFERENCES position_sheets,
  side TEXT, price NUMERIC, qty INT,
  executed_at TIMESTAMPTZ, slippage_bps NUMERIC

TABLE outcomes
  sheet_id TEXT PK REFERENCES position_sheets,
  entry_price NUMERIC, exit_price NUMERIC,
  holding_period_s INT,
  pnl_krw NUMERIC, pnl_pct NUMERIC,
  exit_reason TEXT  -- tp|sl|breakeven|scale_out|time|profit_floor|death_cross|overextension|manual|unfilled

TABLE scout_runs
  scout_run_id TEXT PK, generated_at TIMESTAMPTZ,
  code_hash TEXT, code_text TEXT,
  hypothesis TEXT, candidates_count INT,
  model_used TEXT, cost_usd NUMERIC

TABLE macro_runs
  macro_run_id TEXT PK, generated_at TIMESTAMPTZ,
  gate TEXT, size_multiplier NUMERIC,
  reasoning TEXT, news_digest_ref TEXT

TABLE meta_prs
  pr_number INT PK, created_at TIMESTAMPTZ,
  risk_category TEXT, stage INT,
  eval_run_id TEXT, merged_at TIMESTAMPTZ,
  reverted_by INT REFERENCES meta_prs
```

### 5.3 Observer 이벤트

Prime Jennie prefix `pj.`:

```
pj.scout.code_generated
pj.scout.code_executed
pj.scout.candidate_passed
pj.macro.gate_closed
pj.strategy.sheet_published
pj.executor.order_placed
pj.executor.order_filled
pj.executor.position_closed
pj.meta.eval_completed
pj.meta.pr_generated
pj.meta.pr_merged
pj.ui.command_issued
pj.ui.command_acknowledged
```

---

## 6. 보안 및 격리

### 6.1 Screening Executor 샌드박스

| 항목 | 설정 |
|---|---|
| 컨테이너 | non-privileged |
| User | non-root |
| 네트워크 | `network: none` |
| 파일시스템 | `/data` read-only, `/tmp` tmpfs |
| 메모리 / CPU | 4GB / 2 core |
| 실행 시간 | 300s timeout, SIGKILL |
| syscall | seccomp profile |

**허용 import**:
```python
ALLOWED_IMPORTS = {
    "pandas", "numpy", "scipy.stats",
    "talib",
    "sklearn.cluster",
    "sklearn.linear_model",
    "sklearn.preprocessing",
    "sklearn.metrics",
    "math", "statistics", "datetime",
}
```

**명시 금지**: `os`, `sys`, `subprocess`, `socket`, `importlib`, `ctypes`, `threading`, `multiprocessing`, 파일 I/O, `sklearn` 루트 import.

**sklearn submodule 단위** 근거: sklearn 전체 허용 시 `sklearn.externals` 등 간접 경로로 보안 우회 가능. submodule로 좁혀 표면 최소화.

### 6.2 GitHub Token Scope

| Token | 권한 | 저장 |
|---|---|---|
| `RUNTIME_CI_TOKEN` | runtime: write | GitHub Actions Secret |
| `META_PR_TOKEN` | **runtime만**: PR create | meta repo env |
| `META_SELF_TOKEN` | **발급 안 함** | — |

`META_PR_TOKEN`은 Fine-grained PAT, repo 하나만 명시.

### 6.3 감사 로그

meta 생성 PR 필수 포함:
- `provenance`: 어떤 eval 결과
- `reverts`: 이전 PR 참조
- `risk_category`: scout_code / eval_logic / executor_rule
- `stage`: 0/1/2/3

### 6.4 Control UI 인증 경계

- **외부 경계**: Cloudflare Access + Google OAuth. 영석 계정만.
- **내부 경계**: 파괴적 액션 추가 one-time PIN.
- **신뢰 경계**: Redis 명령 경로. UI/Telegram 발행자 사전 검증. Executor는 출처 무관 신뢰.

---

## 7. prime-jennie-meta 상세 설계

### 7.1 디렉토리 구조

```
prime-jennie-meta/
├── README.md, AGENTS.md, pyproject.toml
│
├── prime_jennie_meta/
│   ├── eval/
│   │   ├── weekly_eval.py
│   │   ├── metrics.py            # Sharpe, MDD, Hit Rate
│   │   └── regime_detector.py
│   │
│   ├── agents/
│   │   ├── eval_analyst.py       # SubAgentRole
│   │   ├── pr_generator.py       # SubAgentRole
│   │   └── prompts/
│   │
│   ├── pr_manager/
│   │   ├── github_client.py
│   │   ├── pr_template.py
│   │   ├── stage_policy.py
│   │   └── revoke.py
│   │
│   ├── sandbox/
│   │   └── runtime_checkout.py   # PR 검증용 clone
│   │
│   └── infra/
│
├── config/
│   ├── stage.yaml
│   └── revoke_log.yaml
│
└── tests/
```

### 7.2 Eval Pipeline (주간, 일요일 02:00 KST)

```
1. Performance DB 지난 7일 수집
2. Sharpe, MDD, Hit Rate, strategy_tag별 PnL
3. Regime Detector: 전주 대비 변화
4. Eval Analyst (SubAgentRole) → 개선 후보 structured output
5. PR Generator: runtime checkout → 수정 → 백테스트 → 통과 시 PR
6. Stage Policy에 따라 auto-merge 판정
```

### 7.3 Stage 관리

```yaml
# config/stage.yaml
current_stage: 0
auto_merge_enabled: false

stage_transitions:
  - from: 0
    to: 1
    required:
      weeks_at_current: 4
      pr_count_manually_approved: 10
      no_revokes_in_last: 14  # days
  - from: 1
    to: 2
    required:
      weeks_at_current: 8
      auto_merged_prs_with_positive_alpha: statistically_significant
  - from: 2
    to: 3
    required:
      # 영구 canary. 자동 승격 없음.
      manual_approval: true
```

**Stage별 권한**:

| Stage | 수정 범위 | 자동 merge |
|---|---|---|
| 0 Shadow | PR 생성만, 전부 수동 | ❌ |
| 1 Scout | Scout 코드 | 백테스트 통과 시 auto, 48h revoke 가능 |
| 2 Eval | Eval 메트릭/로직 | auto |
| 3 Executor | 매매 로직 | **영구 수동**, paper 1주 + 소액 1주 + 영석 승인 |

### 7.4 PR 템플릿

```markdown
## Summary
<한 문장>

## Risk Category
- [ ] scout_code
- [ ] eval_logic
- [ ] executor_rule

## Stage
Current: Stage <N> / Auto-merge: <yes/no>

## Provenance
- Eval run: eval_20260420_0200
- Triggered by: <메트릭/regime/백테스트>
- Data: Sharpe X→Y, MDD X%→Y%
- 자연어 설명

## Backtest Result
<요약 + 리포트 링크>

## Reverts
<이전 PR 링크>

## Human Review Required
- [ ] Stage 0
- [ ] 리뷰 권장: ...
```

---

## 8. 개발 로드맵

### Phase 0: 설계 (현재)
- 이 문서 v0.2 + 세부 스펙 3~4종
- 2~3일

### Phase 1: runtime 스켈레톤 (5 Track 병렬)

**Track A — 인프라/스캐폴딩**
- repo 초기화, pyproject, docker-compose
- Redis Stream, Observer, Langfuse
- Postgres 스키마 마이그레이션

**Track B — 느린 루프**
- Scout/Macro/Strategy Agent
- 포지션 시트 pydantic (schema v1.1)

**Track C — 빠른 루프 + v2 포팅**
- Executor 프레임
- KIS Gateway (FastAPI)
- Telegram Bot (Redis pub)
- Risk Throttle, Overextension Filter

**Track D — Screening Executor**
- Docker, seccomp, 화이트리스트
- `ScreeningToolAdapter`
- 악의 코드 테스트 ≥10

**Track E — v2 데이터 & News Pipeline**
- v2 MariaDB → v3 Postgres 마이그레이션
- `stock_minute_prices` read-only 마운트
- News Pipeline KOR 포팅 + Qdrant

완료 조건: Scout placeholder → 포지션 시트 → Executor 모킹 체결.

**3주 (5 Track 병렬)**.

### Phase 2: runtime 실전화
- Scout 프롬프트 튜닝
- 백테스트 엔진 완성
- KIS paper trading
- WSJ Macro Gate
- v2 ↔ v3 공존 시작

**3주**. 완료 조건: 1주 paper에서 v2 대비 동등 이상.

### Phase 2.5: Control UI (Phase 2와 병렬 가능)
- Next.js 스캐폴딩
- Cloudflare Access
- 대시보드 / 포지션 / Scout 페이지
- /control 긴급 제어

**2주**.

### Phase 3: meta 스켈레톤
- Eval Pipeline, PR Generator (Stage 0)
- Meta PR 센터 UI 연동

**2주**.

### Phase 4: Stage 0 Shadow
- 최소 4주. PR 쌓기만.

### Phase 5: Stage 1~2
- 승격당 8주 관찰.

### Phase 6: Stage 3 (영구 canary)
- v2 은퇴.

---

## 9. Claude Code 병렬 개발 가이드

### 9.1 Phase 1 Track 경계

| Track | 소유 | 다른 Track은 |
|---|---|---|
| A | `infra/`, `migrations/` | read only |
| B | `slow_loop/`, `position_sheet/` | `schema.py` read only |
| C | `fast_loop/`, `kis_gateway/`, `telegram_bot/` | 건드리지 않음 |
| D | `screening_executor/` | adapter B에서 import만 |
| E | `news_pipeline_kor/`, `migrations/` | A와 `migrations/` 공유, 사전 조율 |

### 9.2 각 Track AGENTS.md 필수 항목

- 디렉토리 책임 (5책임 중 어디)
- 건드리면 안 되는 것
- 테스트 실행법
- 의존 프로토콜
- **v2 포팅 Track(C, E)은 원본 v2 경로 명시**

### 9.3 공유 스펙 (변경 시 stop-the-world)

- `POSITION_SHEET_SPEC.md`
- `MACRO_GATE_SPEC.md`
- `SCOUT_CODE_GENERATION.md`
- `DB_SCHEMA.md` (신설)

### 9.4 Control UI

`prime-jennie-control-ui` 별도 repo. Phase 2.5에서 단일 Track.

### 9.5 Claude Code 초기 프롬프트 템플릿

```
너는 prime-jennie-runtime의 Track <X> 담당이다.
필독:
1. /docs/prime_jennie_v3_phase0_design.md (v0.2)
2. /docs/POSITION_SHEET_SPEC.md
3. /<track>/AGENTS.md

책임: <...>
건드리면 안 되는 파일: <...>
minyoung-mah는 pip로만 소비. 직접 import 금지.
v2 포팅 Track이면 원본 v2 경로: <...>
커밋은 하나의 논리 단위. 테스트 없는 커밋 금지.
```

---

## 10. Open Questions

1. **Scout 코드 언어**: Python 화이트리스트, 문제 시 DSL 재검토.
2. **meta Eval 주기**: 주간 먼저, Stage 1 후 일간.
3. **v2 공존 기간**: Phase 2 paper 병행 → Phase 6까지 백업 유지.
4. **Harness 0.2.0 gap**: local workaround 우선, 2회 반복 시 Harness PR.
5. **Stage 승격 자동화**: Stage 3 이후 별도 논의.
6. ~~비용 상한~~ **CLOSED**: $50 soft / $100 hard. Phase 2 실측 후 재조정.
7. **뉴스 latency**: News 5분 이내 vs Scout 1일. 안전. Phase 2 재확인.

---

## 11. Non-goals

- **HFT**: 초 단위로 충분.
- **크립토**: KOSPI 현물만.
- **외부 사용자**: 단일 사용자.
- **meta의 meta**: meta 자체 개선은 수동.

(v0.1의 "UI 안 만든다" 삭제. Control UI + Telegram 긴급 제어는 in-scope.)

---

## 12. 승인 체크리스트 (v0.2)

**v0.1 승인 7항목**: 유지.

**v0.2 신규**:

- [ ] 컨테이너 예산 10개 → **13개** 상향
- [ ] Telegram 긴급 제어 in-scope
- [ ] `prime-jennie-control-ui` 별도 repo
- [ ] Control UI 스택(Next.js 15 + TS + Tailwind + shadcn/ui)
- [ ] **Cloudflare Access 인증 필수** (민지 강력 권장)
- [ ] 포지션 시트 `exit.rules[]` 확장 (breakeven + scale_out)
- [ ] v2 데이터 마이그레이션 전략 (§13)
- [ ] 비용 상한 $50 soft / $100 hard

승인 후 세부 스펙 3종 작성:
- `POSITION_SHEET_SPEC.md`
- `SCOUT_CODE_GENERATION.md`
- `MACRO_GATE_SPEC.md`

---

## 13. v2 → v3 데이터 마이그레이션

v2 축적 자산을 v3 cold start 해결에 활용.

### 13.1 v2 자산 재고

| 테이블 | 규모 | v3 활용 |
|---|---|---|
| `stock_minute_prices` | 50만건+ | **read-only 마운트**. 마이그 없음. v3 백테스트가 직접 쿼리. |
| `stock_news_sentiments` | 수만건 | Qdrant 재임베딩 후 v3 append. 기존 컬렉션 유지+확장. |
| `signal_logs` | 2만건+ | `legacy_signal_logs`로 마이그. meta Eval 학습 데이터. |
| `daily_quant_scores` | 수천건 | `legacy_quant_scores`. Scout 팩터 baseline. |
| `trade_logs` | 실거래 | `legacy_trade_logs`. v2 baseline. |

### 13.2 전략

**원칙**:
- 일방향(v2 MariaDB → v3 Postgres). 역류 없음.
- v2 테이블 read-only 유지.
- `legacy_` 접두사로 네임스페이스 분리.

**산출물** (Track E):
```
migrations/
├── 001_v3_schema.sql
├── 002_legacy_namespace.sql
├── migrate_signal_logs.py
├── migrate_trade_logs.py
├── migrate_daily_quant_scores.py
└── verify_migration.py       # row count + hash
```

### 13.3 공존 기간

**Phase 2 ~ Phase 6**: v2 MariaDB + v3 Postgres 동시 운영.

- v2 MariaDB: v2 paper trading이 계속 씀.
- v3 Postgres: v3 운영 전체.
- 동기화 없음. 독립 운영.
- Phase 6 진입 시 v2 은퇴, MariaDB cold archive.

### 13.4 파일 기반 자산

v2 `runtime_state.json`, `risk_state.json`:
- v3 **Redis에 통합**. 마이그 없음.
- 초기값은 영석 수동 (기본값 기준).

---

**문서 끝.**
