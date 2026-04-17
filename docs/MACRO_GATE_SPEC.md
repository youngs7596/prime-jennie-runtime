# MACRO_GATE_SPEC

> **문서 목적**: Macro Gate 에이전트의 전수 명세. 바이너리 게이트 원칙, 입력 소스, 판정 기준, 프롬프트 전략, 실패 처리까지.
>
> **선행 문서**: `prime_jennie_v3_phase0_design.md` §4.3, §1.1
>
> **작성자**: 민지 × 영석
> **작성일**: 2026-04-16
> **버전**: 0.2

---

## CHANGELOG

### v0.2 (2026-04-16)
Claude Code v2 컨텍스트 리뷰 반영. 이산화 경계값 모호성 제거.

- **§2.2 이산화 테이블 half-open interval로 재정의**: `(low, high]` 방식. 경계값은 아래 구간 포함. 정확히 0.25는 0.25, 0.2500001은 0.50.
- **§2.2 구현 의사코드 추가**: `discretize()` 함수 전체 공개. clamp + 구간 매핑.
- **§2.2.1 open + size=0.0 모순 처리 신설**: 0.25로 강제 보정 + `pj.macro.inconsistent_open_zero` 이벤트 + Telegram 알림.
- **§4.1 System Prompt 제약 보강**: "gate=open과 size=0.0은 동시 불가" 명시.
- **§9 테스트 케이스 8건 추가** (MG15~MG22): 경계값, clamp, 모순 시나리오.

### v0.1 (2026-04-16)
초안.

---

## 1. 개요

### 1.1 Macro Gate의 책임

Macro Gate는 거시 환경을 해석하여 **두 개의 숫자**만 출력한다:

1. `gate`: `"open"` | `"closed"`
2. `size_multiplier`: 0.0 ~ 1.0

다른 모든 출력(reasoning, top_risks 등)은 **로깅 용도**이며 **실행 경로에 절대 참조되지 않는다**.

### 1.2 반(反) 어드바이저 원칙

v2 Council의 구조적 문제: 매크로 판단이 자연어로 풍부하게 나오면, 그걸 파싱해서 실행 로직에 끼워넣고 싶은 유혹이 생긴다. 이 유혹을 구조적으로 차단.

**설계 규칙**:
- `reasoning` 필드는 Executor가 참조 불가 (코드 레벨로 강제)
- 실행 로직에 영향을 미치는 건 오직 `gate`와 `size_multiplier` 두 숫자
- 이 원칙 위반 PR은 meta 자동 거부 (§7)

### 1.3 실행 주기

- **정시 실행**: 매일 **08:00 KST** (장 시작 전)
- **Ad-hoc**: 영석이 Control UI `/macro`에서 수동 트리거 가능
- **자동 트리거**: 다음 조건 시
  - 장중 KOSPI -3% 이상 급락 (15분봉 기준)
  - KRW/USD 환율 ±1.5% 급변
  - VIX 30 초과 (직전 값 대비 20% 급등)

자동 트리거 시 `macro_runs.trigger_reason` 기록.

### 1.4 모델

- Tier: `REASONING` (qwen3-max)
- 근거: 지정학, 정책 연쇄, 시장 구조 등 다층적 추론 필요

---

## 2. 출력 스키마

### 2.1 MacroGateOutput

```python
class MacroGateOutput(BaseModel):
    # === 실행 로직 참조 가능 ===
    gate: Literal["open", "closed"]
    size_multiplier: float          # 0.0 ~ 1.0
    
    # === 로깅 전용 (실행 로직 참조 금지) ===
    reasoning: str                  # 자연어 판단 근거 (500자 이내)
    top_risks: list[RiskItem]       # 최대 5개
    confidence: Literal["high", "medium", "low"]
    news_digest_ref: str            # news_digests 테이블 FK
    next_review_hint: str | None    # "6시간 후 재검토 권장" 등
```

```python
class RiskItem(BaseModel):
    category: Literal[
        "geopolitical", "monetary", "liquidity",
        "sector_contagion", "fx", "commodity", "other"
    ]
    description: str                # 80자 이내
    severity: Literal["critical", "high", "medium", "low"]
```

### 2.2 size_multiplier 구간 매핑

Macro Gate가 출력하는 `size_multiplier`는 연속값(0.0 ~ 1.0)이지만, 실제 운용은 다음 이산 구간으로 매핑된다.

**매핑 규칙 (half-open interval, 올림 방식)**:

| LLM 출력 `x` 범위 | 실제 적용 값 | 의미 |
|---|---|---|
| `gate == "closed"` | **0.0** | 신규 진입 금지 (`x` 무시) |
| `x == 0.0` | **0.0** | — (open + 0.0은 비정상. §2.2.1 참조) |
| `0.0 < x <= 0.25` | **0.25** | 극도로 보수 |
| `0.25 < x <= 0.50` | **0.50** | 보수 |
| `0.50 < x <= 0.75` | **0.75** | 중립 |
| `0.75 < x <= 1.0` | **1.0** | 적극 |
| `x > 1.0` | **1.0** | clamp (비정상 출력 방어) |
| `x < 0.0` | **0.0** | clamp (비정상 출력 방어) |

**경계값 처리**: 정확히 0.25는 첫 구간에 속하여 **0.25**로 매핑. 정확히 0.50은 두 번째 구간에 속하여 **0.50**. 일반 원칙: **경계값은 아래 구간에 포함** (half-open `(low, high]`).

**구현 의사코드**:

```python
def discretize(x: float, gate: str) -> float:
    if gate == "closed":
        return 0.0
    
    # clamp
    x = max(0.0, min(1.0, x))
    
    if x == 0.0:
        # open + 0.0은 정의상 비정상. 로그 후 0.25로 강제.
        emit_observer("pj.macro.inconsistent_open_zero")
        return 0.25
    
    # half-open (low, high]
    if x <= 0.25: return 0.25
    if x <= 0.50: return 0.50
    if x <= 0.75: return 0.75
    return 1.0
```

**왜 half-open `(low, high]`인가**: LLM이 소수점 둘째 자리 이상의 정밀한 값을 내놓을 이유가 없음. 경계값 관측이 발생하면 대부분 "더 보수적으로" 의도한 경우가 많으므로, 하위 구간에 포함시켜 정확히 해당 안전 수준 적용.

**이유**: LLM이 0.73이라고 내놓든 0.78이라고 내놓든 실제 차이가 무의미하며, 이산화가 일관성을 준다. v2에서 Council의 position_size_pct가 70/73/78로 갈리며 디버깅 시간 낭비했던 경험을 반영.

**예외**: `gate == "closed"`일 때 `size_multiplier` 값은 무시되고 무조건 0.0.

#### 2.2.1 open + size_multiplier 0.0 처리

`gate == "open"`인데 `size_multiplier == 0.0`인 출력은 **논리적으로 모순**이다 (열렸지만 크기 0). 처리:

- `discretize()`가 0.25로 강제 보정
- `pj.macro.inconsistent_open_zero` observer 이벤트
- 영석 Telegram 알림 (프롬프트 품질 이슈 시그널)

Macro Gate 프롬프트에도 이 제약을 명시: "gate=open과 size_multiplier=0.0은 동시에 내보낼 수 없다. 0으로 판단되면 gate=closed."

### 2.3 gate == "closed" 조건

Macro Gate는 다음 중 **하나라도** 충족 시 `"closed"`를 출력해야 한다:

1. **지정학적 critical 이벤트**: 한반도/중동/대만 군사 충돌 임박 또는 발생
2. **유동성 경색**: 전일 대비 호가 스프레드 2배 이상 확대, KOSPI 거래대금 50% 이상 감소
3. **섹터 전염**: 주요 섹터 3개 이상 동시에 -5% 이상 하락
4. **환율 충격**: KRW/USD 일일 ±3% 이상 변동
5. **시스템 이벤트**: 증권사 시스템 장애, 서킷브레이커 발동 당일

1개 미만 충족 시 `"open"`. 이 조건은 프롬프트에 명시적으로 들어간다.

---

## 3. 입력 데이터

### 3.1 News Digest (WSJ)

**소스**: WSJ 뉴스레터 → Gmail → Macro News Digest Pipeline

**파이프라인**:
```
Gmail API (WSJ 뉴스레터 구독 계정)
  ↓ 10분마다 폴링
뉴스레터 본문 파싱
  ↓
중요 기사 추출 (헤드라인 + 요약)
  ↓
LLM 요약 (tier: DEFAULT, qwen3.5-plus)
  ↓
news_digests 테이블 적재
```

**news_digests 스키마**:
```sql
TABLE news_digests
  digest_id TEXT PRIMARY KEY,      -- "wsj_20260416_0730"
  collected_at TIMESTAMPTZ NOT NULL,
  source TEXT NOT NULL,            -- "wsj_newsletter"
  headlines JSONB,                 -- [{title, summary, relevance}]
  macro_summary TEXT,              -- LLM 요약 (500자 이내)
  raw_text TEXT                    -- 원본 (감사용)
```

### 3.2 News Digest (국내)

**소스**: `news_pipeline_kor` (§prime_jennie_v3_phase0_design.md §4.9)

**사용 방식**: Macro Gate 실행 시 국내 뉴스 중 **매크로 관련** 카테고리(금통위, 환율, 지정학, 정책)만 필터링해서 digest 생성.

news_pipeline_kor는 ticker 단위로 감성 점수를 내지만, Macro Gate는 ticker 무관 매크로 이슈에 관심. 별도 쿼리 함수:

```python
def get_macro_relevant_kor_news(as_of: datetime, hours: int = 24) -> list[dict]:
    """
    카테고리 필터: 금통위, 환율, 지정학, 정책, 거시경제
    ticker 무관
    """
```

### 3.3 시장 지표 스냅샷

Macro Gate 실행 시 수집:

```python
class MarketSnapshot(BaseModel):
    as_of: datetime
    
    # 국내
    kospi: IndexPoint
    kosdaq: IndexPoint
    kospi_200_futures: IndexPoint | None
    
    # 해외 (전일 종가 또는 현재 선물)
    sp500: IndexPoint
    nasdaq: IndexPoint
    nikkei: IndexPoint
    hsi: IndexPoint
    
    # 환율 / 원자재
    usd_krw: float
    usd_jpy: float
    crude_oil: float
    gold: float
    
    # 공포 지수
    vix: float
    vkospi: float | None
    
    # 변동성
    kospi_20d_vol: float
    kospi_60d_vol: float

class IndexPoint(BaseModel):
    close: float
    change_pct: float
    volume: float | None
```

### 3.4 직전 Macro Gate 이력

```python
class RecentMacroRun(BaseModel):
    macro_run_id: str
    generated_at: datetime
    gate: str
    size_multiplier: float
    top_risks_summary: str          # top_risks를 한 줄로 압축
```

최근 7일간의 run을 프롬프트에 주입. 판단 연속성 확보.

---

## 4. 프롬프트 전략

### 4.1 System Prompt

```
당신은 Prime Jennie의 Macro Gate입니다. KOSPI/KOSDAQ 트레이딩 시스템의 거시 환경 게이트 역할을 합니다.

책임:
- 거시 환경을 종합 판단하여 오늘 매매 허용 여부(gate)와 포지션 크기 배수(size_multiplier)를 결정한다
- 판단 근거(reasoning)는 명확히 기록하지만, 이는 로깅 용도이며 실행 로직은 두 숫자(gate, size_multiplier)만으로 결정된다

중요 원칙:
1. 당신은 어드바이저가 아닙니다. 두 숫자로 게이트 역할만 합니다.
2. 불확실성을 reasoning으로 풀어쓰려 하지 말고, size_multiplier의 수치로 표현하십시오.
3. "상황을 지켜봐야 한다" 같은 유보적 표현 대신, 현재 가용 정보로 결정하십시오. 6시간 후 재검토는 next_review_hint에 적으십시오.

gate = "closed" 조건 (하나라도 충족 시 반드시 closed):
1. 지정학적 critical 이벤트 (한반도/중동/대만 군사 충돌 임박 또는 발생)
2. 유동성 경색 (호가 스프레드 2배 확대, 거래대금 50% 감소)
3. 섹터 전염 (주요 섹터 3개 이상 동시 -5% 이상 하락)
4. 환율 충격 (KRW/USD 일일 ±3% 이상)
5. 시스템 이벤트 (시스템 장애, 서킷브레이커)

1개 미만 충족 시 open. 이 조건은 엄격히 지킵니다.

size_multiplier 가이드:
- 1.00: 거시 매우 양호, 변동성 낮음, 뚜렷한 상승 환경
- 0.75: 중립 ~ 약한 긍정
- 0.50: 불확실, 혼재 신호
- 0.25: 부정적이나 closed 조건 미충족, 극히 보수적 운용
- 출력은 연속값이지만 0.25 단위로 이산화됨을 감안 (경계값은 아래 구간에 포함, 즉 0.50 출력은 0.50으로 매핑)

중요 제약 — 모순 방지:
- gate="open"과 size_multiplier=0.0은 동시에 내보낼 수 없습니다. 크기가 0이면 정의상 closed입니다.
- 0에 가까운 값이면 size_multiplier=0.01 이상으로 내놓되 reasoning에 보수 이유 명시, 또는 gate=closed로 전환하십시오.

top_risks는 최대 5개. 각 리스크는 category/severity/description 구조.
severity "critical"이 1개 이상이면 gate 판단에 강하게 반영.

reasoning은 500자 이내. 결론→근거 순서. 감정어 배제, 사실과 판단만.

과거 run 연속성 고려:
- 제공되는 최근 7일 run 이력을 참고하십시오
- 급격한 게이트 전환(전일 1.00 → 오늘 0.25)은 명확한 사유와 함께
- 반대로 상황이 명확히 개선되었는데도 관성적으로 낮은 multiplier를 유지하지 마십시오
```

### 4.2 User Prompt 템플릿

```
## 분석 기준 시각
{as_of}
Trigger: {trigger_reason}   # "scheduled_0800" | "manual" | "auto_kospi_drop" 등

## 시장 스냅샷

국내:
- KOSPI: {kospi_close} ({kospi_change:+.2%}), 거래대금 {kospi_value}조
- KOSDAQ: {kosdaq_close} ({kosdaq_change:+.2%})

해외 (전일 / 현재 선물):
- S&P500: {sp500_close} ({sp500_change:+.2%})
- Nasdaq: {nasdaq_close} ({nasdaq_change:+.2%})
- Nikkei: {nikkei_close} ({nikkei_change:+.2%})
- HSI: {hsi_close} ({hsi_change:+.2%})

환율/원자재:
- USD/KRW: {usd_krw} ({usd_krw_change:+.2%})
- Crude WTI: {crude} ({crude_change:+.2%})
- Gold: {gold} ({gold_change:+.2%})

공포 지수:
- VIX: {vix} (전일 {vix_prev})
- VKOSPI: {vkospi}

변동성:
- KOSPI 20d vol: {kospi_20d_vol:.1%}
- KOSPI 60d vol: {kospi_60d_vol:.1%}

## WSJ News Digest (최근 24시간)
Digest ID: {wsj_digest_id}
{wsj_macro_summary}

주요 헤드라인:
{wsj_headlines_formatted}

## 국내 매크로 뉴스 (최근 24시간)
{kor_macro_news_formatted}

## 최근 7일 Macro Gate 이력

{recent_macro_runs_table}

---

다음 형식으로 응답하십시오:

{{
  "gate": "open" | "closed",
  "size_multiplier": 0.0 ~ 1.0,
  "reasoning": "<500자 이내>",
  "top_risks": [
    {{
      "category": "geopolitical" | "monetary" | ...,
      "description": "<80자 이내>",
      "severity": "critical" | "high" | "medium" | "low"
    }}
  ],
  "confidence": "high" | "medium" | "low",
  "news_digest_ref": "{wsj_digest_id}",
  "next_review_hint": "<없으면 null>"
}}
```

### 4.3 Few-shot 예제

3개 시나리오:

1. **평온한 정상 시장** → `gate=open, size=1.00`
2. **미중 긴장 + KOSPI 약세** → `gate=open, size=0.50`, risks에 geopolitical + sector_contagion
3. **이란-이스라엘 충돌 발생일 아침** → `gate=closed, size=0.0`, risks에 geopolitical critical

각 예제는 user prompt + 이상적 output 쌍. 토큰 예산 각 400토큰 이내.

### 4.4 Structured Output

`with_structured_output(MacroGateOutput)`. 파싱 실패 시 3회 재시도.

### 4.5 프롬프트 버전 관리

- `macro_prompt_v{N}.md` Git 관리
- `macro_runs.prompt_version` 컬럼
- few-shot 별도 버전

---

## 5. 판정 로직

### 5.1 LLM 출력 후 처리

```python
async def run_macro_gate(context: MacroContext) -> MacroGateOutput:
    raw_output: MacroGateOutput = await llm_call(...)
    
    # 1. closed 조건 재검증
    closed_triggers = check_closed_conditions(context.market_snapshot)
    if closed_triggers and raw_output.gate == "open":
        # LLM이 조건을 놓쳤음. 강제로 closed로 전환.
        raw_output = raw_output.model_copy(update={
            "gate": "closed",
            "size_multiplier": 0.0,
            "reasoning": f"[AUTO-OVERRIDE] 자동 closed 조건 충족: {closed_triggers}. "
                         f"원본 LLM 판단: {raw_output.reasoning}",
        })
        emit_observer("pj.macro.auto_override")
    
    # 2. size_multiplier 이산화
    if raw_output.gate == "closed":
        raw_output.size_multiplier = 0.0
    else:
        raw_output.size_multiplier = discretize(raw_output.size_multiplier)
    
    # 3. 이력 연속성 체크 (경고만)
    if abrupt_transition(raw_output, recent_runs):
        emit_observer("pj.macro.abrupt_transition")
    
    return raw_output
```

### 5.2 check_closed_conditions (결정론적)

LLM 판단과 별개로 **코드가** 매크로 closed 조건을 재검증. LLM이 놓쳐도 코드가 잡아냄.

```python
def check_closed_conditions(snap: MarketSnapshot) -> list[str]:
    triggers = []
    
    if snap.usd_krw_change_abs >= 0.03:
        triggers.append("fx_shock")
    
    if snap.kospi_20d_vol >= 0.35:
        triggers.append("high_volatility")
    
    # 섹터 전염: 별도 sector_snapshot 조회
    major_sector_drops = count_major_sector_drops(snap.as_of)
    if major_sector_drops >= 3:
        triggers.append("sector_contagion")
    
    # 지정학: 코드로 판단 불가, LLM에 위임
    # 유동성: 호가 스프레드는 추가 데이터 필요 (Phase 2 추가)
    
    return triggers
```

결정론 판정과 LLM 판정이 **독립적**이어야 한다. 둘 다 closed를 말하면 확실하고, 하나만 말하면 auto-override.

### 5.3 이력 연속성 체크

```python
def abrupt_transition(current: MacroGateOutput, history: list[RecentMacroRun]) -> bool:
    if not history:
        return False
    prev = history[0]  # 직전 run
    delta = abs(current.size_multiplier - prev.size_multiplier)
    return delta >= 0.5  # 한 단계 건너뛴 급격한 변화
```

급격한 전환이 **잘못된 건 아니지만** (이란 전쟁 같은 상황), 로깅으로 남겨 영석이 리뷰 가능하게.

---

## 6. Storage 및 전파

### 6.1 macro_runs 테이블

```sql
TABLE macro_runs
  macro_run_id TEXT PRIMARY KEY,     -- "macro_20260416_0800"
  generated_at TIMESTAMPTZ NOT NULL,
  trigger_reason TEXT NOT NULL,       -- scheduled_0800 | manual | auto_*
  gate TEXT NOT NULL,
  size_multiplier NUMERIC NOT NULL,
  reasoning TEXT,
  top_risks_json JSONB,
  confidence TEXT,
  news_digest_ref TEXT REFERENCES news_digests,
  next_review_hint TEXT,
  
  -- 메타
  prompt_version TEXT,
  model_used TEXT,
  cost_usd NUMERIC,
  latency_ms INT,
  auto_override_applied BOOLEAN DEFAULT FALSE
```

### 6.2 Redis pub/sub

```
CHANNEL: macro.state
PAYLOAD: {
  "macro_run_id": "macro_20260416_0800",
  "gate": "open",
  "size_multiplier": 0.75,
  "generated_at": "2026-04-16T08:00:12+09:00"
}
```

**구독자**:
- Strategy Engine: 매 Scout run 전 최신 상태 확인
- Executor (빠른 루프): 장중 closed 전환 시 신규 진입 즉시 차단
- Telegram Bot: `confidence == "low"` 또는 `auto_override_applied == true` 시 영석 알림
- Control UI: 대시보드 Macro 상태 카드 갱신

### 6.3 현재 상태 저장

```
REDIS KEY: macro:current_state
TTL: 없음 (항상 최신 값 유지)
VALUE: 직전 macro_runs의 JSON snapshot
```

Strategy Engine이 Scout run 시작 시 이 키를 읽어 `macro_state_snapshot`에 포함.

### 6.4 Stale Detection

`macro:current_state`의 `generated_at`이 **24시간 이상 오래**되면:
- Strategy Engine은 포지션 시트 발행 **중단**
- `pj.macro.stale_detected` 이벤트 + Telegram 알림
- 영석이 수동 ad-hoc 실행 필요

---

## 7. meta 개입 경계

### 7.1 Stage별 권한

POSITION_SHEET_SPEC §5.2 Stage 2(`eval_logic`) 진입 시 meta가 일부 영역 자동 수정 가능:

| 영역 | Stage 0 | Stage 1 | Stage 2 | Stage 3 |
|---|---|---|---|---|
| Macro 프롬프트 자연어 | 수동 | 수동 | **auto** | auto |
| closed 조건 임계값 | 수동 | 수동 | 수동 | 수동 |
| size_multiplier 이산화 테이블 | 수동 | 수동 | 수동 | 수동 |
| Few-shot 예제 교체 | 수동 | 수동 | **auto** | auto |

### 7.2 meta가 건드릴 수 없는 것

- **반(反) 어드바이저 원칙 위반 코드**: meta가 `reasoning`을 실행 로직에 연결하는 PR을 생성하면 **자동 거부**.
- **auto_override 로직**: LLM 출력을 덮어쓰는 결정론 코드는 영석만 수정.
- **discretize 테이블**: Stage 3에서도 수동.

**자동 거부 규칙** (PR CI에 포함):
```python
# meta PR이 다음 패턴을 포함하면 자동 reject
FORBIDDEN_PATTERNS = [
    r"macro.*reasoning.*\b(if|match)",    # reasoning을 조건문에
    r"parse.*macro.*reasoning",           # reasoning 파싱
    r"reasoning.*\.contains\(",           # reasoning 내용 체크
]
```

---

## 8. 실패 처리

### 8.1 Gmail 접근 실패

- WSJ 뉴스레터 수집 실패
- 3회 재시도, 지수 백오프
- 최종 실패 시 **직전 digest 재사용** (최대 24시간 이내만)
- 24시간 초과 시 digest 없이 Macro Gate 실행 + `news_digest_ref: "unavailable"`
- Telegram 경고

### 8.2 LLM 호출 실패

- 3회 재시도
- 최종 실패 시 **직전 Macro Gate 상태 유지**
- `pj.macro.llm_failed` 이벤트 + Telegram 알림
- 스케줄 실행 실패 시 1시간 후 자동 재시도

### 8.3 LLM 출력 파싱 실패

- structured output 스키마 불일치
- 3회 재시도 (프롬프트에 이전 실패 사유 추가)
- 최종 실패 → §8.2와 동일 처리

### 8.4 auto_override 발동

`check_closed_conditions`가 트리거를 잡았는데 LLM이 open을 내놓았을 때:
- 자동으로 closed로 덮어씀
- Telegram **즉시 알림** (영석 리뷰 필요)
- 다음 회차에 프롬프트 개선 또는 few-shot 업데이트 후보

### 8.5 시장 데이터 수집 실패

- 국내 지표: KRX 개장 전 데이터 부재는 정상. 전일 종가 사용.
- 해외 지표: 데이터 소스 실패 시 직전 값 사용 + `data_freshness_warning` 필드
- **모든 주요 지표 부재** 시 Macro Gate 실행 보류, 영석에게 수동 실행 요청

---

## 9. 테스트 케이스 (Track B 필수)

| # | 케이스 | 예상 |
|---|---|---|
| MG01 | 평온 시장 정상 입력 | `gate=open, size=1.00` 또는 0.75 |
| MG02 | KRW/USD +3.2% 급변 | `gate=closed` (auto-override 발동 가능) |
| MG03 | LLM이 closed 조건 놓침 | auto_override_applied=true, Telegram 알림 |
| MG04 | size_multiplier=0.73 출력 | discretize → 0.75 |
| MG05 | size_multiplier=0.26 출력 | discretize → 0.50 |
| MG06 | gate=closed with size=0.3 | size 강제로 0.0 |
| MG07 | 전일 1.00 → 오늘 0.25 | abrupt_transition 이벤트 |
| MG08 | 24시간 stale 상태 | stale_detected, Scout 발행 중단 |
| MG09 | LLM 3회 실패 | 직전 상태 유지, 알림 |
| MG10 | WSJ digest 부재 | news_digest_ref="unavailable", 실행 계속 |
| MG11 | top_risks 6개 반환 | validator 실패, 재시도 |
| MG12 | reasoning 800자 | validator 실패, 재시도 |
| MG13 | Redis pub 후 Strategy Engine 갱신 확인 | 통합 테스트 |
| MG14 | auto 트리거 (KOSPI -3.2%) | ad-hoc run, trigger_reason=auto_kospi_drop |
| MG15 | size_multiplier 정확히 0.25 | discretize → 0.25 (경계값, 아래 구간 포함) |
| MG16 | size_multiplier 정확히 0.50 | discretize → 0.50 (경계값) |
| MG17 | size_multiplier 정확히 0.75 | discretize → 0.75 (경계값) |
| MG18 | size_multiplier 정확히 1.00 | discretize → 1.00 |
| MG19 | size_multiplier 1.2 (비정상) | clamp → 1.00 |
| MG20 | size_multiplier -0.1 (비정상) | clamp → 0.0, open이면 inconsistent 알림 |
| MG21 | gate=open + size=0.0 (모순) | discretize → 0.25 강제, inconsistent_open_zero 이벤트 |
| MG22 | size_multiplier 0.2500001 | discretize → 0.50 (half-open: > 0.25면 상위 구간) |

---

## 10. 관찰 지표

### 10.1 일일 / 주간 모니터링

- `macro_gate_closed_rate`: 최근 30일 중 closed 비율
- `auto_override_rate`: LLM과 결정론 판정 불일치 비율
- `abrupt_transition_count`: 주간 급격 전환 수
- `avg_confidence`: 주간 평균 confidence 분포
- `avg_size_multiplier`: open 상태 평균 배수

### 10.2 품질 지표 (meta가 사용)

- **판단 후행 정확도**: gate=closed였던 날 실제 KOSPI 하락 여부
- **missed calls**: gate=open이었는데 당일 -3% 이상 급락한 경우
- **false alarms**: gate=closed였는데 당일 +2% 이상 상승한 경우

Stage 2 진입 시 meta가 이 지표로 프롬프트 조정 PR 생성.

---

## 11. News Digest Pipeline 보조 명세

### 11.1 WSJ 구독 계정 설정

- 전용 Gmail 계정 (`pj-macro-news@gmail.com` 등)
- WSJ 아침 뉴스레터, Markets AM, Central Banking 등 구독
- Gmail API OAuth 인증, refresh token은 Kubernetes secret 또는 env

### 11.2 news_digests 생성 플로우

```
1. Gmail API search: 지난 24시간 내 WSJ 발신 메일
2. 본문 HTML 파싱 (BeautifulSoup)
3. 광고/서명 제거
4. 헤드라인 + 요약 추출
5. LLM(qwen3.5-plus)로 매크로 관련성 스코어링 + 한글 요약
6. news_digests 테이블 INSERT
```

### 11.3 중복 방지

같은 뉴스가 여러 뉴스레터에 등장 가능. `digest_id`는 timestamp-hour 단위. 같은 시간대 중복 실행 시 UPSERT.

### 11.4 보안

- Gmail 계정은 **읽기 전용** OAuth scope만
- 2FA 필수
- 계정 유출 시 매크로 판단 오염 가능 — 접근 감사 로그 필수

---

## 12. 장기 확장 (v1.2+ 고려)

**현재 out of scope**이나 설계 시 고려:

- **지역별 세분화 gate**: KOSPI/KOSDAQ 별도 게이트
- **섹터별 size_multiplier**: 전체 1.0이지만 반도체 0.5 같은 미세조정
- **시간대별 게이트**: 오전 1.0, 오후 0.75
- **실시간 재계산**: 현재 08:00 + ad-hoc → 2시간 간격 자동 재계산

이 확장들은 **단순성을 해칠 위험**이 있음. v3 안정 운용 후 실제 필요 확인 후 검토.

---

**문서 끝.**
