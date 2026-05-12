# 검토 의뢰서 — prime-jennie 의 다음 진화 단계

작성일: 2026-05-12
대상: 외부 reviewer (Claude Web)
작성자: prime-jennie-runtime 운영자 + Claude Code (CLI)

---

## 0. 한 줄 요약

한국 주식 long-only 자동매매 시스템 (prime-jennie-runtime v3, 약 6주 운영) 의 현재 구현과 운영자의 비전 (LLM 이 사람처럼 종합 판단해 주도적으로 투자) 을 받아, 점진적 발전 path 가 합리적인지 검토 부탁드립니다.

## 0.1 시간 없으면 여기만 답해도 됩니다 (핵심 질문 5)

1. **비전 자체의 타당성**: long-only 자동매매 시스템이 "LLM 이 거시 약세 자율 판단 → STOP/Inverse, 압도적 conviction 종목엔 포트폴리오 재조정해서 대폭 진입, swap 로직" 으로 진화하는 게 합리적인가? 아니면 long-only 의 구조적 한계 안에서 다른 방향이 더 낫나?

2. **4-phase path 의 ROI 순서**: 우리가 제안한 (1) conviction→size → (2) Inverse ETF → (3) advisory swap → (4) Strategy LLM full swap 순서가 합리적인가? 다른 순서나 빠뜨린 단계가 있나?

3. **Strategy LLM 도입의 위험**: 현재 Strategy Engine 은 **결정론** (LLM 0, 모두 yaml + python). 사이즈/룰을 LLM 판단으로 옮기면 비결정성 + 변동성 ↑. 어디까지 LLM, 어디까지 결정론으로 둬야 하나? 안전 가드는?

4. **Inverse ETF 헷지의 실효성**: long-only 계좌에서 KODEX 인버스 (122630), 인버스 2X (252670) 같은 종목으로 short exposure 합성하는 게 실제 알파를 만들 수 있나? 아니면 그냥 STOP/현금이 더 나은가? (KIS 신용/대차 없음, 진짜 short 불가)

5. **빠뜨린 큰 risk**: 우리가 인지하지 못한 구조적 위험 (regulatory / execution / capacity / behavioral) 이 있나?

---

## 1. 시스템 개요 — prime-jennie-runtime v3

### 1.1 2-tier 아키텍처

```
slow_loop (5분 cycle)              fast_loop (tick driven)
┌────────────────────┐            ┌─────────────────────────┐
│ MacroGate          │            │ PositionSheetConsumer   │
│  └ DeepSeek 평가   │            │  └ 발행된 sheet 소비    │
│ Scout              │            │ BalanceAwareSizer       │
│  └ DeepSeek 코드   │            │  └ 자산비례 + cap        │
│    생성 + sandbox  │            │ EntryConditionEvaluator │
│ StrategyEngine     │ ─sheet→    │  └ price/RSI/volume    │
│  (결정론, LLM X)   │  Redis     │ EntryExecutor → KIS 매수│
│  └ candidate→sheet │            │ PositionTracker         │
└────────────────────┘            │ ExitEvaluator           │
                                  │  └ fixed_sl/trailing_tp│
                                  │ ExitExecutor → KIS 매도 │
                                  └─────────────────────────┘
```

- **slow_loop**: 5분 cron (장 09:00~15:30) + auto-trigger (KOSPI -3% / VIX 30 / FX ±1.5%). Macro gate → Scout → StrategyEngine 직렬.
- **fast_loop**: Redis pub/sub 으로 sheet 소비. tick (KIS WebSocket + REST poll). 1분봉 bar engine 으로 RSI / volume MA / recent high 산출.

### 1.2 LLM 사용 위치

| 컴포넌트 | 모델 | 역할 |
|---|---|---|
| MacroGate | DeepSeek (3-LLM council) | "거시 open/closed + size_multiplier" 판단. KOSPI/VIX/FX/뉴스 종합 |
| Scout | DeepSeek | 매일 N회 "오늘의 screening hypothesis + python 코드 생성". 코드 sandbox 실행하면 ScreeningCandidate list 반환 |
| News classifier | Qwen3-30B (vLLM 자체호스팅) | 한국 뉴스 → event_type (earnings/mna/lawsuit/...) + impact (high/mid/low) |
| Briefing | DeepSeek | 운영 요약 보고 (사용자에게 일 1~2회) |
| StrategyEngine | — (LLM X, 결정론) | candidate → sheet 변환. yaml policy 기반 |

→ **현재 LLM 권한은 "후보 발굴" 과 "거시 판단" 까지. 사이즈/룰 결정은 결정론.**

### 1.3 보유 자산 / 거래 규모 (2026-05-12 기준)

- 총자산 **208.6M KRW** (현금 182.4M + 평가 26.2M)
- 활성 종목 7, 자산비례 12% cap (1종목 max ≈25M)
- 일 평균 거래: 매수 5~15건 / 매도 10~30건 (대부분 부분체결 fragment 또는 손절)
- 일 손익 변동: ±0.5~2% 수준

### 1.4 strategy_tag (현재 운용)

| tag | base_pct | krw_cap | 의도 |
|---|---|---|---|
| GAP_UP_REBOUND | 12% | 50M | 갭상승 후 분봉 돌파 확인 진입, 단기 모멘텀 |
| SECTOR_MOMENTUM | 12% | 50M | 섹터 20일 모멘텀 상위 |
| EARNINGS_DRIFT | 12% | 50M | 어닝 직후 5~10일 PED |
| MEAN_REVERT_RSI | 8% | 30M | RSI 28↓ 반등 (좁은 범위) |

`(deprecated)` RSI_REBOUND.

### 1.5 안전 layer (현재)

1. **Macro Gate `closed`** → sheet 발행 0
2. **Risk Throttle (intraday)** WARNING/CRITICAL → size_multiplier 자동 축소 (오늘 0.60)
3. **자산비례 12% cap** → 종목당 max ≈25M
4. **fast_loop entry 가드**: rsi_under (1분봉 RSI 과열 차단), price_above_recent_high (직전봉 돌파 확인), spread_under_bps, price_above
5. **fixed_sl 5%** → 모든 종목 자동 손절
6. **STOP / PAUSE / DRYRUN** Redis 키 (수동 또는 텔레그램)

---

## 2. 운영 실측 — 2026-05-12 (KOSPI -2.29% 일)

### 2.1 시스템 결과

| 지표 | 값 |
|---|---|
| KOSPI | -2.29% |
| 우리 일일 변동 | **-0.55%** (총자산 기준) |
| 실현 PnL | -953K KRW (매도 41건, 96% 가 정확히 -5% 손절) |
| 평가 PnL | -181K KRW (보유 7종 평균 -0.69%) |
| 매수 | **0건** (버그 — 후술) |

### 2.2 오늘의 버그 (사고 보고)

- 5-11 batch 의 Scout prompt v0.3 가 LLM 에게 schema 와 다른 형식 (`trailing_stop`, `time_stop{days: N}`) 을 가르쳤음
- position_sheet schema 는 `trailing_tp` + `time_stop{mode, value}` 만 받음
- 5-12 09:30 첫 scout_run 부터 모든 candidate → ValidationError → 매수 sheet 발행 0
- **결과적으로 KOSPI -2.29% 일에 매수 0 이 우연한 풀헷지로 작용** (들어갔으면 거의 다 -5% 손절)
- 동일 cycle 의 매도 41건은 fast_loop 자체 exit rule 로 정상 작동 → 손실은 -5% 에서 컷
- fix 완료: prompt v0.4 (schema 정렬) + engine `_normalize_scout_exit_hint()` 방어 layer + 회귀 테스트 3종. 100건 candidate replay 100% 통과

### 2.3 본질적 관찰

오늘이 보여준 것: **이 시스템은 bear day 에 "덜 잃는다" 까지만 한다.** "벌지는 못한다." long-only momentum + 5% 손절 패턴은 down 추세에서 burn rate 만 늦춘다.

---

## 3. 운영자의 비전 (검토 핵심)

운영자 본인 표현:

> "하락이 예상된다면 stop을 걸거나, 인버스를 사고, 아주 확실하게 좋아보이는 종목을 찾아내게 된다면, 가지고 있는 포트폴리오를 일부 조정해서 그 종목의 포지션을 대폭 늘려서 확실하게 들어가고, 사람이 이런저런 상황판단을 하면서 투자를 하는것 처럼, llm이 주도적으로 상황과 숫자들을 인간보다 똑똑하게 판단하면서 투자를 대신 더 잘 해주는 것"

핵심 3 원칙:
1. **거시 자율성**: 하락 예상 시 LLM 이 자율 STOP / Inverse 진입 결정
2. **차별적 확신 (conviction-weighted)**: 압도적 확신 종목엔 포트폴리오 재조정해서 대폭 진입
3. **사람처럼 종합 판단**: 룰 기반 자동매매가 아닌, LLM 의 종합적 의사결정 위임

---

## 4. 현재 시스템 vs 비전 — 4 gap

### Gap #1. Macro 권한 부족

| | 현재 | 비전 |
|---|---|---|
| state | open / closed / manual_override (3) | + bear / inverse_recommended / portfolio_rebalance |
| action | size_multiplier 만 조정 | STOP 자동 SET, INVERSE sheet 발행 |
| 누가 결정 | LLM 이 판단, 사용자가 액션 | LLM 이 판단+액션 |

**현재**: KOSPI -2.29% 인데도 macro 는 `open` 유지 (LLM 의 보수적 open 편향). 사용자가 수동 STOP.

### Gap #2. Conviction → size 미반영

```python
# 현재 (engine.py)
final_pct = base_pct × macro_multiplier × risk_multiplier
# conviction 은 candidate 선택 기준일 뿐, sizing 에 안 들어감
```

```python
# 비전
final_pct = base_pct × macro_multiplier × risk_multiplier × conviction_curve(c)
# conviction_curve: c=0.95+ 면 1.8×, c=0.55 면 0.5× (단 12% cap 상한 유지)
```

**현재**: Scout 가 conviction 0.95 와 0.60 candidate 를 같은 사이즈로 매수. 정보의 일부 손실.

### Gap #3. 포트폴리오 재조정 (swap) 메커니즘 없음

- 현재: 매도 (exit rule) ↔ 매수 (sheet 발행) **완전 독립**
- 비전: "이 종목 trim 하고 저 종목 늘리기" 같은 swap 로직
- 사례: 보유 005935 (삼전우) -3% 이고 신규 conviction 0.95 종목 발견 시 → 자동 swap 권고 / 실행

이 gap 은 가장 어려움. 결정론 sheet-by-sheet 구조를 깸. **포지션 매니저 layer** 신규 필요.

### Gap #4. Strategy 결정 영역의 LLM 부재

현재 LLM 의사결정:
- Macro gate (open/closed/size_mult)
- Scout (candidate 발굴)
- News 분류

결정론 (LLM 0):
- Strategy Engine (sheet 조립)
- BalanceAwareSizer (qty 계산)
- ExitEvaluator (exit decision)
- EntryConditionEvaluator (entry gating)

**비전**: "오늘의 종합 plan" 을 LLM 이 발행 — 어떤 종목 진입 / 어떤 보유 trim / 어떤 인버스 추가, 한 의사결정으로 통합.

위험: 비결정성 ↑, debugging 난이도 ↑, capacity 제한 (LLM rate limit).

---

## 5. 제안 path — 4 phase

| Phase | gap | 코드량 | 위험 | ROI |
|---|---|---|---|---|
| **Phase A (next)** | Gap #2 conviction → size | ~30줄 | 매우 낮음 (cap 상한 유지 시 max risk 동일) | ★★★ 단기 |
| **Phase B** | Gap #1 INVERSE_HEDGE strategy_tag + Macro 자동 trigger | ~150줄 | 중 (인버스 ETF 의 실효성 검증 필요) | ★★★ bear 알파 |
| **Phase C** | Gap #4 일부 — Scout hypothesis 에 "포트 재조정 advisory" 자유텍스트 (사용자 검토 후 수동 반영) | ~50줄 | 낮음 (advisory 만, 실행 X) | ★★ 데이터 축적 |
| **Phase D** | Gap #3 + Gap #4 통합 — 포지션 매니저 + Strategy LLM full swap | ~500줄+ | 높음 (비결정성, LLM 판단 오류 → 실손) | ★ 비전 완성 |

### Phase A 상세 (가장 작은 첫 step)

```yaml
# strategy_policy.yaml 추가
conviction_curve:
  type: piecewise_linear
  points:
    - [0.50, 0.50]   # conviction 0.50 이하 → 50% size
    - [0.70, 1.00]   # 0.70 → 100%
    - [0.90, 1.50]   # 0.90 → 150%
    - [1.00, 2.00]   # 1.00 → 200%
```

```python
# engine.py (BalanceAwareSizer)
final_pct = base_pct * macro_mult * risk_mult * curve.lookup(candidate.conviction)
# clamp to MIN_POSITION_PCT ~ MAX_ASSET_PCT (12% 자산 cap 상한)
```

→ 1 commit + 4 테스트로 완료. 기존 sheet 발행 path 변경 X.

### Phase B 상세

1. yaml 에 `INVERSE_HEDGE` strategy_tag 추가:
   ```yaml
   INVERSE_HEDGE:
     base_pct: 0.05
     krw_cap_default: 20_000_000
     universe: ["122630", "252670"]  # KODEX 인버스, KODEX 200선물인버스2X
     default_exit_rules: [...]
   ```

2. Macro gate 출력에 `inverse_recommended: bool` + `inverse_size_pct` 추가
3. StrategyEngine 이 macro state 가 `inverse_recommended=True` 면 universe 의 인버스 종목에 sheet 자동 발행
4. Scout 와 독립된 macro-driven sheet path. (Scout 와 충돌 우려 없음)

### Phase C 상세 (LLM advisory)

Scout 의 output schema 에 `portfolio_rebalance_advice` 자유텍스트 필드 추가. LLM 이 "보유 005935 -3% 인데 conviction 떨어지면 trim 권고" 같은 텍스트를 첨부. 텔레그램 알림으로 사용자에게 전달. 실행은 사용자 수동.

→ 데이터만 축적, 학습 후 Phase D 의 LLM 판단 정확도 평가에 사용.

### Phase D 상세

Strategy LLM (DeepSeek) 신규 — 다음 input 받음:
- 현재 portfolio state (보유 종목 + 평가손익)
- Scout candidates (top-N)
- Macro state
- Risk throttle
- 어제까지의 outcome 히스토리

output: "오늘의 plan" — 신규 진입 N건 (사이즈 포함), trim K건, hold M건, inverse 진입 J건.

→ 결정론 Validator 가 받아서 schema/cap/duplicate 검증 후 실행.

위험 mitigation:
- daily LLM 비용 cap
- LLM 출력의 변동성 → "급격한 portfolio turnover" 차단 룰 (e.g. 1일 turnover < 30%)
- LLM 판단 vs 결정론 단순 룰 의 backtest 비교 (Phase D 시작 전 필수)

---

## 6. 외부 reviewer 에게 보고 싶은 추가 질문

### 6.1 long-only 의 한계

KIS API 는 한국 개인 신용대차 short 불가. 우리가 할 수 있는 헷지:
- KODEX 인버스 ETF 매수 (long position 으로 short exposure 합성)
- 현금 비중 ↑
- KOSDAQ 인버스 (251340) / 200 선물인버스 2X (252670) 등 레버리지 인버스

**질문**: 이런 ETF 들의 추적오차 / 운용보수 / 일일 리밸런싱 효과 (특히 레버리지 2X) 가 단기 헷지 도구로 실효성 있나? 아니면 그냥 STOP 후 현금이 더 나은가?

### 6.2 시스템의 capacity 한계

현재 자산 208M. 비전대로 작동 시 capacity 한계는?
- 종목당 12% cap → 25M
- 시장 충격 (slippage) — 거래량 적은 종목 + 큰 주문 = 슬리피지
- ETF 인버스 진입 시 만약 자산이 1B 이상 가면 25M ↑ → 시장가 슬리피지

**질문**: 자산 규모별 capacity vs 비전 한계는?

### 6.3 LLM 판단의 검증 방법

비전대로 LLM 이 거시 STOP / 인버스 / swap 까지 결정하면, 판단 정확도를 어떻게 검증하나?
- backtest? — 과거 데이터로 LLM 호출 비용 비싸고 시점 누설 위험
- shadow 운영? — LLM 결정과 결정론 운영을 병행하며 비교
- 사용자 검토? — 매일 사용자가 LLM 의 plan 을 검토 후 승인

**질문**: 어떤 검증이 가장 신뢰성 있나?

### 6.4 운영자의 인지 부하

비전대로 작동해도 사용자가 "LLM 의 판단을 검증" 할 수 있는 시야가 필요. 현재 텔레그램 알림 + 대시보드 + macro briefing.

**질문**: LLM 자율도가 올라갈수록 사용자 검증 부담은 어떻게 변하는가? 필요한 관찰 도구는?

---

## 7. 부록

### 7.1 주요 코드 경로

- `prime_jennie_runtime/slow_loop/pipeline.py` — slow loop 메인 직렬화
- `prime_jennie_runtime/slow_loop/macro/` — Macro Gate
- `prime_jennie_runtime/slow_loop/scout/` — Scout (prompt + code generation + sandbox)
- `prime_jennie_runtime/slow_loop/strategy/engine.py` — Strategy Engine (결정론)
- `prime_jennie_runtime/slow_loop/strategy/strategy_policy.yaml` — yaml policy
- `prime_jennie_runtime/fast_loop/` — fast loop (entry/exit executor, tracker)
- `prime_jennie_runtime/position_sheet/schema.py` — PositionSheet pydantic schema
- `prime_jennie_runtime/kis_gateway/` — KIS REST + WebSocket adapter
- `prime_jennie_runtime/control/` — STOP/PAUSE/DRYRUN consumer + state

### 7.2 최근 30일 큰 변화

- 4-17: Phase 1 Track B 완성 (slow loop 첫 구동)
- 4-21: Qwen3-30B 뉴스 분류 전환
- 4-25: Scout 뉴스 피드 재설계 (NewsEventEntry)
- 5-08: Sizing fix #1 (base_pct 상향)
- 5-10: Sizing fix #2 (자산비례 12% cap 도입, max_notional_pct)
- 5-11: 30 gap audit + 9-agent 병렬 fix (KIS 안전 게이트, v2 scanner 포팅, slow-loop control.state, Macro auto-trigger, Backtest DB 영속화 등 28건)
- 5-12: Scout prompt v0.3 → v0.4 (오늘의 버그 fix)

### 7.3 운영 메트릭 (대표 1주)

- 일 평균 sheet 발행: 5~30건
- 일 평균 매수 체결: 3~15건 (cap 적용 후)
- 일 평균 매도 체결: 10~40건 (부분체결 fragment 포함)
- 평균 보유 기간: 1~5일
- 평균 손절율: 4~6% (fixed_sl 5% 가 dominant)
- 평균 익절율: 3~8%
- 일 손익 변동: ±0.5~2%

### 7.4 비용

- DeepSeek 일 약 $0.5~2 (Macro 3 cycle + Scout 9~12 cycle + Briefing 2)
- vLLM (Qwen3-30B) 자체호스팅 — 전기/하드웨어만
- KIS API 무료 (개인계좌)

### 7.5 운영자 배경

- 한국 SW 엔지니어, 자기 자산으로 운영 중
- v2 (단일 컨테이너 monolith) 약 6개월 운영 후 v3 (멀티 컨테이너, slow+fast loop) 로 점진 마이그레이션
- 현 단계: v2 잔존 기능 일부 포팅 진행 중 + v3 안정화

---

## 8. 마무리

검토 의뢰의 핵심은 **"운영자가 그리고 있는 그림이 long-only 자동매매의 자연스러운 진화인가, 아니면 long-only 구조 안에서 다른 방향이 더 합리적인가"** 입니다.

특히 **Phase B (Inverse ETF) 와 Phase D (Strategy LLM)** 가 실제 알파를 만들 수 있는지에 대한 비판적 검토 부탁드립니다.

검토 결과로 받고 싶은 형태:
1. 각 Phase 의 합리성 평가 (continue / modify / abandon)
2. 빠뜨린 위험 / 대안 path
3. (가능하면) 비슷한 시스템의 사례 또는 학술 reference

감사합니다.
