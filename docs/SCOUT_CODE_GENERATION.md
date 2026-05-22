# SCOUT_CODE_GENERATION

> ## ⚠️ 폐기 (Deprecated) — 2026-05-22
>
> 본 문서는 v3 의 **1차 Scout 아키텍처** (LLM 에게 Python 스크리닝 코드를 생성시키는 방식) 명세입니다.
> 2026-05-22 에 이 접근은 폐기됐고, v2 의 결정론 quant 스코어러로 복원됐습니다.
>
> 폐기 사유:
> - 2026-05-15 같은 거래일 -5% 손절 종목 재매수 사고 → 신뢰 회복 안 됨
> - v3 본격 가동(2026-05-06) 이후 실현 순손실 약 -1,377만원 (132건, 승률 27%)
> - 비결정성을 코드 생성 단계에서마저 제거하기로 결정
>
> 현재 Scout 코드 (결정론 7팩터 quant): [`prime_jennie_runtime/slow_loop/scout/`](../prime_jennie_runtime/slow_loop/scout/)
> - `deterministic_scout.py` — 오케스트레이터 (`run_deterministic_scout`)
> - `quant.py` — 7팩터 스코어러 (v2 포팅)
> - `enrichment.py` — universe 적재
> - `selection.py` — MA 평활 + 히스테리시스
>
> 폐기 결정 기록: [`.ai/decisions/2026-05-22-selection-architecture-decision.md`](../.ai/decisions/2026-05-22-selection-architecture-decision.md)
> 라이브에 남아있던 LLM 코드 생성 잔재 파일들 (`role.py`, `prompts.py`, `code_loop.py`, `code_hasher.py`, `screening_stub.py`) 은 dead-path 로 미사용 — 향후 정리 예정.
>
> 본 문서는 **역사 자료**로만 보존됩니다. 현재 아키텍처는 README.md §"Scout 선정 아키텍처" 참조.
>
> ---

> **문서 목적**: Scout 에이전트가 스크리닝 Python 코드를 생성하는 전 과정의 명세. 프롬프트, 입출력, 샌드박스 계약, 실패 처리, 품질 평가까지.
>
> **선행 문서**: `prime_jennie_v3_phase0_design.md` §4.2, `POSITION_SHEET_SPEC.md` §6
>
> **작성자**: 민지 × 영석
> **작성일**: 2026-04-16
> **버전**: 0.2 (2026-05-22 deprecated)

---

## CHANGELOG

### v0.2 (2026-04-16)
Claude Code v2 컨텍스트 리뷰 반영. v2 축적 컨센서스 데이터 접근 경로 명확화.

- **§2.3 `consensus_data` DataFrame 신설**: 기존 `market_data`와 별개로 ticker별 컨센서스 스냅샷 DataFrame을 Scout 코드에 제공. `forward_per`, `forward_eps`, `eps_revision_1m`, `analyst_count` 등 포함.
- **§2.3 Stale 처리 기준 추가**: `consensus_data.last_updated` 14일 이상 오래된 ticker는 제외 권장.
- **§4.1 함수 시그니처 context 명세 갱신**: `context["consensus_data"]` 접근 경로 명시.
- **§4.3 예제 코드 갱신**: consensus 데이터 사용 패턴 (reindex + NaN 안전 처리) 포함. 팩터 4개 결합 (momentum, volume, news, eps_revision).

### v0.1 (2026-04-16)
초안.

---

## 1. 개요

### 1.1 Scout의 책임

Scout는 **자연어로 종목을 추천하지 않는다**. 시장 데이터에 **적용될 Python 스크리닝 코드**를 생성한다.

**이유**:
- 자연어 스코어는 하류에서 재해석 비용 발생
- 환각된 종목명이 그대로 매매로 이어질 수 있음
- 코드는 백테스트 엔진이 즉시 검증 가능
- 동일한 코드를 과거 시점에 실행해 점검 가능 (재현성)

### 1.2 실행 흐름

```
Scout Agent
   │
   │ LLM 호출 (structured output)
   v
ScoutOutput {
  screening_code: str
  hypothesis: str
  ...
}
   │
   v
Screening Executor (격리 컨테이너)
   │
   │ exec(screening_code)
   v
list[ScreeningCandidate]
   │
   v
Strategy Engine → 포지션 시트 발행
```

### 1.3 주기

- **기본 주기**: 1일 1회, 장 시작 전 08:30 KST
- **Ad-hoc 트리거**: 매크로 상태 급변 시 영석이 수동 실행 가능 (Control UI `/scout` 페이지)
- **빈도 상한**: 최소 2시간 간격. 과도한 호출 방지 (비용 + 과최적화 방지)

### 1.4 모델

- Tier: `STRONG` (qwen3-coder-next)
- 대안: `REASONING`(qwen3-max). Scout가 복잡한 가설 추론 필요 시 영석이 명시적으로 선택.

---

## 2. 입력 데이터

### 2.1 Context 객체

Scout Agent가 LLM 호출 시 system/user 프롬프트에 포함하는 데이터:

```python
class ScoutContext(BaseModel):
    as_of: date                              # 분석 기준일 (보통 today)
    universe: list[str]                      # 분석 대상 ticker 리스트
    market_summary: MarketSummary            # 지수, 섹터 수익률
    macro_state: MacroStateSnapshot          # 최신 Macro Gate 결과
    news_scores: dict[str, NewsScoreEntry]   # ticker → 감성 점수
    sector_momentum: dict[str, float]        # 섹터 → 20일 모멘텀
    consensus_estimates: dict[str, dict]     # ticker → 컨센서스
    previous_scout_runs: list[ScoutRunSummary]  # 최근 5회 Scout run 요약
    strategy_tags_available: list[str]       # 사용 가능한 strategy_tag
```

### 2.2 universe 구성 규칙

- **기본**: KOSPI 200 + KOSDAQ 150 = 350 종목
- **제외**:
  - 관리종목, 투자경고
  - 거래정지
  - 상장 90일 미만 (신규 상장 변동성 회피)
  - 시가총액 1,000억 원 미만
  - 20일 평균 거래대금 10억 원 미만 (유동성)

영석이 `config/universe_policy.yaml`에서 조정 가능. 변경은 다음 Scout run부터 반영.

### 2.3 market_data 제공 방식

Scout Agent 자체는 DataFrame을 프롬프트에 넣지 **않는다** (토큰 낭비). 대신 Scout가 생성한 코드가 Screening Executor에서 **실행 시점에** DataFrame을 받는다.

Screening Executor는 Scout 코드에 **두 개의 DataFrame**을 제공한다:

```
market_data: pd.DataFrame                 # 일봉 시계열
  인덱스: MultiIndex(ticker, date)
  컬럼: open, high, low, close, volume, value,
        market_cap, foreign_ratio, institution_net_buy,
        ma5, ma20, ma60, rsi14, bb_upper, bb_lower
  기간: as_of - 60일 ~ as_of

consensus_data: pd.DataFrame              # 컨센서스 스냅샷 (ticker별 1행)
  인덱스: ticker (단일)
  컬럼: forward_per, forward_eps, forward_roe,
        target_price, analyst_count,
        eps_revision_1m, eps_revision_3m,
        target_price_revision_1m,
        last_updated
  기간: as_of 시점의 최신 컨센서스 1행
```

**consensus_data 제공 방식**:
- Scout 생성 코드의 `screen(market_data, context)` 함수는 **`context["consensus_data"]`로 접근**
- 시그니처는 변경 없음 (context dict 안에 DataFrame 포함)
- v2의 `daily_quant_scores` 테이블에서 축적된 데이터가 공급원

**프롬프트 명시**:
```
context["consensus_data"]: pd.DataFrame
  인덱스: ticker
  컬럼: forward_per, forward_eps, forward_roe,
        target_price, analyst_count,
        eps_revision_1m, eps_revision_3m,
        target_price_revision_1m, last_updated
  
  주의: 컨센서스 데이터가 없는 ticker는 DataFrame에 없음.
        접근 시 .get 또는 try/except 대신 .reindex()로 NaN 처리 권장.
```

**Stale 처리**: `consensus_data.last_updated`가 as_of 대비 14일 이상 오래된 ticker는 Scout 코드가 제외 권장. 프롬프트에서 안내.

### 2.4 news_scores 구조

```python
class NewsScoreEntry(BaseModel):
    score: float              # -1.0 ~ +1.0
    timestamp: datetime       # 가장 최신 반영 뉴스 시각
    article_count: int        # 집계에 포함된 기사 수
    staleness_hours: float    # as_of - timestamp (시간 단위)
```

**Stale 처리**: `staleness_hours > 48`인 ticker는 Scout 코드가 기본적으로 제외하는 게 권장. 프롬프트에서 안내하지만 강제하지 않음.

---

## 3. 출력 스키마

### 3.1 ScoutOutput (LLM structured output)

```python
class ScoutOutput(BaseModel):
    screening_code: str           # 실행 가능한 Python 코드
    code_hash: str                # sha256, Scout Agent가 아닌 Agent 레이어에서 계산
    hypothesis: str               # 자연어 가설 (200자 이내)
    expected_candidates: int      # 예상 통과 종목 수 (Scout의 자기 추정)
    factor_weights: dict[str, float]  # 사용 팩터와 상대 가중치
    strategy_tags_used: list[str] # 코드가 생성하는 strategy_tag (복수 가능)
    fallback_strategy: str        # 통과 종목 0개일 때 대응
    estimated_runtime_seconds: float  # 코드 실행 예상 시간
```

### 3.2 ScreeningCandidate (Scout 생성 코드의 반환 타입)

```python
class ScreeningCandidate(BaseModel):
    ticker: str                   # "005930"
    strategy_tag: str             # POSITION_SHEET_SPEC §2.3 enum
    conviction: float             # 0.0 ~ 1.0, Scout 코드가 자체 판단
    entry_hint: EntryHint         # Strategy Engine이 참고
    exit_hint: ExitHint | None    # 없으면 strategy_tag 기본값 사용
    factors: dict[str, float]     # 이 종목이 통과한 팩터 값들
    notes: str                    # 짧은 사유 (100자 이내)
    thesis_spec: ThesisSpec | None  # G6 thesis_aware_hold (2026-05-17, prompt v0.8 추가)
```

```python
class EntryHint(BaseModel):
    trigger: Literal["limit", "market"]
    price_hint: float | None      # limit일 때만. Strategy Engine이 여기에 +/-조정 가능
    conditions_hint: list[dict]   # POSITION_SHEET_SPEC §4.2 conditions
```

```python
class ExitHint(BaseModel):
    rules_hint: list[dict]        # POSITION_SHEET_SPEC §5 rules
```

```python
class ThesisSpec(BaseModel):
    """검증 가능한 hypothesis 조건 — Phase A 영속, Phase 1 advisory, Phase 2 enforce.

    Scout LLM 이 hypothesis 자연어와 함께 condition list 를 생성. revaluator
    (slow_loop/thesis/, Phase 1 5-22~) 가 보유 sheet 의 conditions 를 정기 평가,
    critical_conditions 깨지면 invalidated → forced_liquidation:thesis Redis SET
    적재 (Phase 2 5-29~).

    Phase A 호환: thesis_spec None 허용. screen() 가 채우지 않으면 revaluator skip.
    """
    natural_language: str         # scout_hypothesis 와 동일 호환
    conditions: list[ThesisCondition]
    critical_conditions: list[int]  # conditions index 리스트

class ThesisCondition(BaseModel):
    type: Literal[
        "kospi_gate", "kospi_change_pct_above", "sector_momentum_above",
        "no_risk_event_high", "earnings_event_window", "rsi_below",
        "price_above_breakout", "r20d_above_threshold",
    ]
    params: dict[str, Any]
```

**catalog v1 (5종, Phase A 측정 후 확장)** — 단순화 결정 [`.ai/designs/2026-05-17-g-series-simplification.md`](../.ai/designs/2026-05-17-g-series-simplification.md):
- `kospi_gate` (macro 종합 판정 open/closed)
- `sector_momentum_above` (섹터 N영업일 누적 모멘텀)
- `no_risk_event_high` (24h high-impact risk_event 부재)
- `earnings_event_window` (earnings event 후 N영업일 이내)
- `rsi_below` (1일봉 RSI 임계 미만)

남은 3종 (`kospi_change_pct_above` / `price_above_breakout` / `r20d_above_threshold`) 은 schema 만 정의, Phase A 1주 측정 (Scout LLM 의 실 thesis_spec 반환률 / catalog 사용 빈도) 후 catalog 편입 또는 제거 결정.

**critical_conditions 선정** — policy-only (LLM 자유 지정 제거):

| strategy_tag | policy critical 후보 |
|---|---|
| GAP_UP_REBOUND | `kospi_gate` (Phase 1 측정 후 sector breakout 추가 검토) |
| SECTOR_MOMENTUM | `kospi_gate`, `sector_momentum_above` |
| EARNINGS_DRIFT | `earnings_event_window`, `no_risk_event_high` |
| MEAN_REVERT_RSI | `rsi_below` |

screen() 함수가 반환하는 모든 candidate 에 동일한 thesis_spec 첨부 (run 단위 thesis 공유). prompt v0.8 (`slow_loop/scout/prompts.py:SCOUT_PROMPT_VERSION = "v0.8"`) 에 catalog 가이드 + 예시 코드 포함.

### 3.3 Scout가 **하지 않는 것**

- **포지션 사이즈 결정**: `size.base_pct`는 Strategy Engine의 `strategy_policy.yaml`에서. Scout는 건드리지 않음.
- **Macro 판단**: `gate` 열림/닫힘은 Macro Gate 소관. Scout는 그 결과를 소비만.
- **Risk Throttle 반영**: risk_multiplier도 Strategy Engine이 시트 발행 시점에 스냅샷.
- **최종 포지션 시트 조립**: 모든 조각을 합치는 건 Strategy Engine.

---

## 4. 생성 코드의 계약

Scout가 생성하는 Python 코드는 **정확히 다음 시그니처**의 `screen` 함수를 정의해야 한다.

### 4.1 함수 시그니처

```python
def screen(market_data: pd.DataFrame, context: dict) -> list[ScreeningCandidate]:
    """
    market_data: MultiIndex (ticker, date) DataFrame
    context: {
      "as_of": date,
      "universe": list[str],
      "news_scores": dict[str, dict],       # ticker → NewsScoreEntry
      "sector_momentum": dict[str, float],
      "consensus_data": pd.DataFrame,        # 인덱스 ticker, 컨센서스 스냅샷 (§2.3)
      "macro_size_multiplier": float,        # 참고용
    }
    
    반환: ScreeningCandidate 리스트
      - 길이 제약: 0 ~ 20개
      - 중복 ticker 금지
      - conviction 내림차순 정렬 권장 (강제 아님)
    """
```

### 4.2 강제 제약 (Screening Executor가 런타임에 체크)

1. **`screen` 함수 정의 필수**. 다른 이름의 엔트리 포인트 불인정.
2. **반환 타입**: `list[ScreeningCandidate]` 또는 `list[dict]` (dict는 ScreeningCandidate로 역직렬화 가능해야 함).
3. **반환 길이 0 ~ 20**. 21개 이상이면 상위 20개만 취함 + 경고.
4. **ticker 중복 금지**. 중복 시 `conviction`이 높은 것만 유지.
5. **universe 밖 ticker 금지**. 위반 시 해당 candidate 필터링.
6. **실행 시간 300초 이내**. 초과 시 SIGKILL.
7. **최상위 레벨 side effect 금지**: `print`, 파일 쓰기, `sys.exit` 등. (허용 import 화이트리스트로 대부분 차단되지만 이중 체크.)

### 4.3 코드 구조 권장 패턴

```python
def screen(market_data, context):
    import pandas as pd
    import numpy as np
    
    as_of = context["as_of"]
    universe = context["universe"]
    news = context["news_scores"]
    consensus = context["consensus_data"]   # pd.DataFrame indexed by ticker
    
    # 1. universe 필터
    df = market_data.loc[universe]
    
    # 2. 팩터 계산
    latest = df.groupby("ticker").last()
    latest["momentum_5d"] = ...
    latest["volume_spike"] = ...
    
    # 3. 1차 필터
    filtered = latest[
        (latest["momentum_5d"] > 0.02) &
        (latest["volume_spike"] > 1.5)
    ]
    
    # 4. 뉴스 스코어 결합
    filtered["news_score"] = filtered.index.map(
        lambda t: news.get(t, {}).get("score", 0.0)
    )
    filtered = filtered[filtered["news_score"] > 0.1]
    
    # 5. 컨센서스 결합 (NaN 안전)
    cons = consensus.reindex(filtered.index)
    filtered["forward_per"] = cons["forward_per"]
    filtered["eps_rev_1m"] = cons["eps_revision_1m"]
    filtered["analyst_cnt"] = cons["analyst_count"]
    
    # 컨센서스 신뢰도 확보: 애널리스트 3명 이상 + 1개월 EPS 상향
    filtered = filtered[
        (filtered["analyst_cnt"] >= 3) &
        (filtered["eps_rev_1m"] > 0)
    ]
    
    # 6. conviction 계산 (팩터 4개 결합)
    filtered["conviction"] = (
        0.3 * filtered["momentum_5d"].rank(pct=True) +
        0.2 * filtered["volume_spike"].rank(pct=True) +
        0.2 * filtered["news_score"].rank(pct=True) +
        0.3 * filtered["eps_rev_1m"].rank(pct=True)
    )
    
    # 7. 상위 N개 선별
    top = filtered.nlargest(15, "conviction")
    
    # 8. ScreeningCandidate 생성
    return [
        {
            "ticker": ticker,
            "strategy_tag": "SECTOR_MOMENTUM",
            "conviction": float(row["conviction"]),
            "entry_hint": {
                "trigger": "limit",
                "price_hint": float(row["close"] * 0.998),
                "conditions_hint": [
                    {"type": "volume_over_ma20", "min_ratio": 1.2}
                ]
            },
            "exit_hint": None,
            "factors": {
                "momentum_5d": float(row["momentum_5d"]),
                "volume_spike": float(row["volume_spike"]),
                "news_score": float(row["news_score"]),
                "eps_rev_1m": float(row["eps_rev_1m"]),
            },
            "notes": f"모멘텀 {row['momentum_5d']:.1%}, EPS↑ {row['eps_rev_1m']:.1%}",
        }
        for ticker, row in top.iterrows()
    ]
```

### 4.4 안티패턴 (Scout 프롬프트에서 명시 금지)

Scout가 생성하면 안 되는 패턴. 프롬프트에 명시:

- 하드코딩된 ticker (e.g. `if ticker == "005930": ...`)
- 미래 데이터 참조 (e.g. `df.shift(-1)`)
- 전역 상태 변조
- 재귀 호출
- 외부 네트워크 호출 시도
- `eval`, `exec`, `compile` 사용
- `__import__` 동적 import
- try/except로 에러 삼키기

---

## 5. 프롬프트 전략

### 5.1 System Prompt

```
당신은 Prime Jennie의 Scout입니다. KOSPI/KOSDAQ 종목 스크리닝을 위한 Python 코드를 생성합니다.

역할:
- 시장 데이터를 분석하여 매수 후보를 필터링하는 `screen` 함수를 작성한다
- 자연어로 종목을 추천하지 않는다. 오직 코드로 표현한다
- 가설(hypothesis)은 한 문단으로 명확히 기술한다

제약:
1. 허용 import만 사용: pandas, numpy, scipy.stats, talib, 
   sklearn.cluster, sklearn.linear_model, sklearn.preprocessing, 
   sklearn.metrics, math, statistics, datetime
2. 네트워크, 파일 I/O, 프로세스, 스레드 관련 모듈 금지
3. eval, exec, compile, __import__ 금지
4. 하드코딩된 ticker 금지. 팩터 기반 필터링만.
5. 미래 데이터 참조 금지 (shift(-n), lookahead)
6. try/except로 에러 숨기기 금지. 의도된 실패는 명시적으로 처리.

생성 코드 요구사항:
- `def screen(market_data: pd.DataFrame, context: dict) -> list[dict]` 시그니처
- 반환 0 ~ 20개
- ticker 중복 금지, universe 밖 ticker 금지
- conviction은 0.0 ~ 1.0

품질 기준:
- 최소 2개 이상의 독립 팩터 결합
- 뉴스 감성은 stale (48시간 초과) 데이터 의존 금지
- 한 종목에 다 걸지 말고 섹터 분산 고려
- fallback_strategy 명시 (통과 종목 0개 대응)

당신의 최근 성과는 provided previous_runs에 있다. 과거 실패 패턴을 피한다.
```

### 5.2 User Prompt 템플릿

```
다음 시장 상황에서 스크리닝 코드를 생성하십시오.

## 분석 기준일
{as_of}

## 시장 요약
- KOSPI: {kospi_close} ({kospi_change:+.2%})
- KOSDAQ: {kosdaq_close} ({kosdaq_change:+.2%})
- 상승종목 / 하락종목: {up_count} / {down_count}

## Macro Gate 상태
- Gate: {macro_gate}
- Size multiplier: {macro_size_multiplier}
- Top risks: {macro_top_risks}

## 섹터 모멘텀 (상위 5 / 하위 5)
{top_sectors_table}
{bottom_sectors_table}

## 뉴스 감성 상위 20종목
{news_top_table}

## 최근 Scout run 요약 (최신 5개)
{previous_runs_summary}

## 사용 가능한 strategy_tag
{strategy_tags_available}

---

다음 형식으로 응답하십시오:

{{
  "screening_code": "<Python 코드>",
  "hypothesis": "<200자 이내 자연어 가설>",
  "expected_candidates": <숫자>,
  "factor_weights": {{"팩터명": 가중치}},
  "strategy_tags_used": ["<tag>"],
  "fallback_strategy": "<짧은 설명>",
  "estimated_runtime_seconds": <숫자>
}}
```

### 5.3 Few-shot 예제

시스템 프롬프트 뒤에 3개 예제 추가. 각 예제는:

1. **GAP_UP_REBOUND 시나리오** — 전일 갭상 + 당일 저가 지지
2. **SECTOR_MOMENTUM 시나리오** — 섹터 상승 + 개별 펀더멘털
3. **MEAN_REVERT_RSI 시나리오** — 과매도 + 뉴스 긍정

각 예제는 input context + 이상적인 ScoutOutput 쌍. 토큰 예산상 각 예제 500토큰 이내로 압축.

### 5.4 구조화된 출력 강제

`with_structured_output(ScoutOutput)` 사용. JSON 파싱 실패 시 3회까지 재시도, 3회 모두 실패면 run 실패 처리.

---

## 6. Screening Executor 샌드박스

### 6.1 컨테이너 구성

`prime-jennie-runtime/screening_executor/` 디렉토리에서 관리. Dockerfile:

```dockerfile
FROM python:3.12-slim

RUN useradd -m -u 1000 screener
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY executor.py .
COPY allowlist.py .
COPY seccomp.json /etc/seccomp.json

USER screener
CMD ["python", "-u", "executor.py"]
```

requirements.txt:
```
pandas==2.2.*
numpy==1.26.*
scipy==1.13.*
ta-lib==0.4.*
scikit-learn==1.5.*
pydantic==2.*
```

### 6.2 컨테이너 실행 인자

```yaml
# docker-compose.yml
screening-executor:
  build: ./screening_executor
  network_mode: none
  read_only: true
  tmpfs:
    - /tmp:size=256m
  volumes:
    - ./data:/data:ro
  mem_limit: 4g
  cpus: 2.0
  security_opt:
    - seccomp=/etc/seccomp.json
    - no-new-privileges:true
  cap_drop:
    - ALL
  user: "1000:1000"
```

### 6.3 Import 화이트리스트 강제

`allowlist.py`에서 **AST 분석으로 import 검사**:

```python
ALLOWED = {
    "pandas", "numpy", "scipy.stats", "talib",
    "sklearn.cluster", "sklearn.linear_model",
    "sklearn.preprocessing", "sklearn.metrics",
    "math", "statistics", "datetime",
}

def check_imports(source: str) -> list[str]:
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not is_allowed(alias.name):
                    violations.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not is_allowed(module):
                violations.append(module)
    # __import__, eval, exec, compile 호출 검사
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "id", None)
            if fn in {"__import__", "eval", "exec", "compile"}:
                violations.append(f"forbidden call: {fn}")
    return violations

def is_allowed(module: str) -> bool:
    return any(module == a or module.startswith(a + ".") for a in ALLOWED)
```

**sklearn 전체 차단**: `is_allowed("sklearn")`는 False. `sklearn.cluster`만 True. `__init__.py` 사이드이펙트 최소화.

### 6.4 executor.py 인터페이스

```python
# executor.py (샌드박스 내부)
import json, sys, ast, hashlib
from allowlist import check_imports

def run(code: str, context: dict) -> dict:
    violations = check_imports(code)
    if violations:
        return {"ok": False, "error": "import_violation", "details": violations}
    
    namespace = {}
    try:
        exec(compile(code, "<scout_code>", "exec"), namespace)
    except Exception as e:
        return {"ok": False, "error": "compile_error", "details": str(e)}
    
    if "screen" not in namespace:
        return {"ok": False, "error": "screen_not_defined"}
    
    # market_data 로드 (read-only mount)
    market_data = load_market_data(context["as_of"])
    
    try:
        result = namespace["screen"](market_data, context)
    except Exception as e:
        return {"ok": False, "error": "runtime_error", "details": str(e)}
    
    # 결과 검증
    if not isinstance(result, list):
        return {"ok": False, "error": "invalid_return_type"}
    if len(result) > 20:
        result = result[:20]
    
    return {"ok": True, "candidates": result}

if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    result = run(payload["code"], payload["context"])
    print(json.dumps(result))
```

### 6.5 ScreeningToolAdapter (Harness 측)

```python
# prime-jennie-runtime 내부, minyoung-mah ToolAdapter 구현
class ScreeningToolAdapter:
    async def invoke(self, code: str, context: dict) -> ScreeningResult:
        # docker run으로 격리 컨테이너 spawn
        # stdin으로 payload 전달, stdout으로 결과 수신
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "-i",
            "--network=none", "--read-only",
            "--memory=4g", "--cpus=2",
            "--security-opt=seccomp=/etc/seccomp.json",
            "--security-opt=no-new-privileges",
            "screening-executor:latest",
            stdin=PIPE, stdout=PIPE, stderr=PIPE,
        )
        
        payload = json.dumps({"code": code, "context": context}).encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload),
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ScreeningResult(ok=False, error="timeout")
        
        return ScreeningResult.model_validate_json(stdout)
```

### 6.6 악의 코드 테스트 케이스 (Track D 필수)

최소 10건 구현. 모두 거부되어야 함:

| # | 공격 벡터 | 예상 결과 |
|---|---|---|
| M01 | `import os; os.system("...")` | import_violation |
| M02 | `__import__("subprocess").call(...)` | forbidden call |
| M03 | `eval("...")` | forbidden call |
| M04 | `exec(b"...".decode())` | forbidden call |
| M05 | `import socket` | import_violation |
| M06 | `open("/etc/passwd")` 내부 | read-only / permission denied |
| M07 | `import sklearn; sklearn.utils...` | import_violation (root) |
| M08 | 무한 루프 `while True: pass` | timeout 300s |
| M09 | 메모리 폭탄 `[0]*10**10` | OOM kill |
| M10 | fork bomb 시도 | seccomp 차단 |
| M11 | `import ctypes` | import_violation |
| M12 | `importlib.import_module("os")` | import_violation |

---

## 7. 백테스트 검증

### 7.1 필수 검증 단계

Scout가 생성한 코드는 **포지션 시트 발행 전** 반드시 백테스트 통과해야 함.

```
screen(today) → candidates
   │
   v
for each candidate:
    historical_screen(past_90_days, candidate.strategy_tag)
    → 과거 동일 팩터로 뽑혔던 종목들의 실제 성과
   │
   v
통과 기준:
    - 과거 90일 유사 셋업의 평균 수익 > 0
    - Sharpe > 0.5
    - 최소 10회 이상 발생한 패턴
```

**통과 못한 candidate**: 해당 candidate만 제외. 전체 Scout run 무효화 아님.

**모든 candidate 실패**: Scout run 실패. `fallback_strategy` 실행 (§8.4).

### 7.2 Look-ahead bias 방지

백테스트 엔진은 각 시점 `t`에서 `t` 시점에 **존재했을** 데이터만 참조. 테스트로 강제:

```python
def test_no_lookahead():
    code = """
def screen(market_data, context):
    df = market_data.groupby("ticker").tail(5)
    # 시도: 미래 데이터
    df["future"] = market_data.groupby("ticker")["close"].shift(-1)
    ...
"""
    result = run_backtest_on(code, as_of=past_date)
    # 미래 컬럼이 NaN이어야 함을 확인
    assert result["future_nan_rate"] > 0.99
```

### 7.3 슬리피지 모델

백테스트는 다음 슬리피지 적용:

```
slippage_bps = 5 + 20 * (order_size / avg_daily_volume_20)
```

실거래 슬리피지가 이보다 크면 meta Eval에서 `backtest_reality_gap` 지표로 플래그.

---

## 8. 실패 처리

### 8.1 LLM 호출 실패

- 네트워크 에러, 토큰 초과, 모델 오류
- 3회 재시도 (지수 백오프: 2s, 8s, 30s)
- 3회 모두 실패 → `pj.scout.llm_failed` 이벤트, run 종료
- 직전 성공 run의 결과를 **재사용하지 않음** (stale 시그널 위험)

### 8.2 코드 생성 실패

- Structured output 파싱 실패
- 필수 필드 누락
- 3회 재시도 (매번 프롬프트에 이전 실패 사유 추가)
- 3회 실패 → run 종료

### 8.3 Screening Executor 실패

```python
match result.error:
    case "import_violation":
        # 영속적 실패. 재시도 안 함. meta가 학습할 신호.
        log_and_fail()
    
    case "compile_error" | "runtime_error":
        # 프롬프트에 에러 메시지 추가 후 1회 재시도
        retry_with_error_context()
    
    case "timeout":
        # 코드가 너무 무거움. 재시도 안 함.
        log_and_fail()
    
    case "invalid_return_type" | "screen_not_defined":
        # 계약 위반. 재시도 안 함.
        log_and_fail()
```

재시도는 **최대 1회**. Scout의 비용 상한 관리.

### 8.4 통과 candidate 0개

- Scout가 명시한 `fallback_strategy` 실행
- 허용 fallback:
  - `"skip_today"`: 오늘 포지션 시트 발행 안 함
  - `"use_previous_run"`: 직전 성공 run의 candidate를 재사용 (단, 24시간 이내)
  - `"relax_filters"`: Scout가 미리 정의한 완화 규칙 적용
- fallback도 실패 시 → 오늘 skip

### 8.5 universe 밖 ticker 환각

- 전체 candidate 중 30% 이상이 universe 밖이면 `pj.scout.hallucination_suspected` 경고
- 50% 이상이면 Scout run 실패 처리 + 영석 Telegram 알림

---

## 9. 품질 평가 (meta가 사용)

### 9.1 주간 메트릭

meta의 Eval Analyst가 주간 집계:

| 메트릭 | 의미 | 개선 방향 |
|---|---|---|
| `scout_hit_rate` | 통과 candidate 중 실제 체결된 비율 | 높을수록 entry_hint 현실적 |
| `scout_pnl_correlation` | conviction vs 실현 PnL 상관계수 | 높을수록 conviction 신뢰 |
| `scout_hallucination_rate` | universe 밖 ticker 생성 비율 | 낮을수록 좋음 |
| `scout_sandbox_failure_rate` | 샌드박스 실행 실패율 | 낮을수록 좋음 |
| `scout_backtest_reality_gap` | 백테스트 예측 vs 실거래 괴리 | 작을수록 좋음 |
| `scout_cost_per_winning_trade` | 수익 거래당 Scout LLM 비용 | 낮을수록 효율적 |

### 9.2 메타가 제안할 수 있는 개선

- 프롬프트 조정 (system prompt rewording)
- Few-shot 예제 교체
- 새 팩터 추가 (context에 팩터 주입)
- 비효율 패턴 블랙리스트 추가

### 9.3 Stage별 권한

POSITION_SHEET_SPEC §5.2 Stage 1(`scout_code`) 진입 시 meta가 이 영역을 자동 수정 가능. 그 전까지는 전부 수동 검토.

---

## 10. 버전 관리 및 롤백

### 10.1 Scout 프롬프트 버전

- `scout_prompt_v{N}.md` 파일로 repo에 보관
- 프롬프트 변경은 Git commit
- 각 Scout run의 `scout_runs.prompt_version` 컬럼에 버전 기록

### 10.2 Few-shot 예제 버전

- `few_shots/v{N}/` 디렉토리
- 예제 교체도 버전 업

### 10.3 롤백 트리거

다음 조건 하나 충족 시 영석 알림 + 이전 버전 제안:

- 주간 `scout_hit_rate` 전주 대비 -30% 이상 하락
- `scout_hallucination_rate` 0.1 초과
- `scout_sandbox_failure_rate` 0.05 초과

### 10.4 롤백 절차

1. 영석이 Control UI `/scout/history`에서 이전 버전 확인
2. Git `checkout` 후 `docker compose restart slow-loop`
3. 롤백 이벤트 observer 기록

---

## 11. 테스트 케이스 (Track B 필수)

| # | 케이스 | 예상 |
|---|---|---|
| S01 | 정상 Scout run | ScoutOutput 유효, 5~15 candidates |
| S02 | 프롬프트 응답이 비JSON | 3회 재시도 후 run 실패 |
| S03 | `screen` 함수 미정의 | compile_error, run 실패 |
| S04 | `import os` 포함 | import_violation, run 실패 |
| S05 | `eval` 호출 포함 | forbidden call, run 실패 |
| S06 | 반환 25개 | 상위 20개만 취함 + 경고 |
| S07 | ticker 중복 | 중복 제거, conviction 높은 것 유지 |
| S08 | universe 밖 ticker 50% | run 실패 + hallucination 알림 |
| S09 | 실행 시간 301초 | timeout, SIGKILL |
| S10 | candidate 0개 | fallback_strategy 실행 |
| S11 | 백테스트 통과율 0 | run 실패 |
| S12 | `scale_out` exit_hint 포함 candidate | Strategy Engine이 포지션 시트에 반영 |
| S13 | 동일 코드 hash 재생성 (재현성) | 같은 결과 확인 |

---

**문서 끝.**
