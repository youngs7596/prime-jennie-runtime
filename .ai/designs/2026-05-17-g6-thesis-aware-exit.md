# G6 Thesis-Aware Exit Design (2026-05-17)

> **⚠️ SIMPLIFIED 2026-05-17 / 2026-05-23 archive 확정** — 5-22 결정론 코어 전환으로 thesis_spec=None 발행, position_sheets 스키마에도 thesis 컬럼 부재. thesis 출처 자체가 끊김. 동기는 살아있으므로 세 갈래 (7 팩터 시계열 / 별도 LLM 채널 / Temporal Context PoC 의 Fact layer 흡수) 중 선택 후 새 design doc — `.ai/designs/2026-05-23-post-llm-at-core-realignment.md` §8.2, §9.
>
> **⚠️ SIMPLIFIED 2026-05-17** — `.ai/designs/2026-05-17-g-series-simplification.md`
> §4 에서 단순화 결정:
> - 명명: G6 → **`thesis_aware_hold`** (의미 기반)
> - catalog 8 → **5종** (kospi_change_pct_above / price_above_breakout / r20d_above_threshold 제거, Phase 1 측정 후 확장)
> - Phase A/B/C → **2 단계** (Phase 1 = schema+revaluator advisory 1주, Phase 2 = enforce). 6-08 → 5-29 단축.
> - 4-state → **2-state** (valid / invalidated, weakened/strengthened 는 Phase 1 측정 후)
> - critical = **policy-only** (LLM × policy intersection 제거)
> - Fail 정책 5종 → **2종** (skip / alert+skip)
>
> Phase A (schema + Scout prompt v0.8) 는 commit 498264d 로 이미 도입 완료 — 그 본문은 archive 로 보관, Phase 1 advisory + Phase 2 enforce 구현 시 본 doc 보다 simplification doc 의 결정 우선.
>
> ---
>
> 본 문서는 같은 날 `2026-05-17-g2-overextension-validator.md` 의 자매 design.
> G2 가 "entry 가드 강화" 라면 본 G6 는 **"hold 가드 신설"** — 직교 축.
>
> 발상의 출처는 사용자가 5-16 Web Claude 와의 대화에서 정리한 "1단 종목 thesis
> 재평가 layer" 아이디어. 그 문서의 거의 모든 처방은 우리 v3 가 이미 G1/G5/Coordinator
> design 으로 해결 또는 진행 중이나, **1단 thesis 객체화 + 4-state 재평가** 만은
> 우리에게 실질적 신규 가치 — 본 design 으로 그 부분만 추출.

## 1. Position in 6-Layer Guards

| # | 가드 | 축 | 위치 | 상태 |
|---|---|---|---|---|
| G1 | Outcome 피드백 | Scout 학습 신호 | Scout context | DONE 5-15 |
| G2 | Overextension validator | **Entry 가드** | Strategy Engine | design 2026-05-17 |
| G3 | 시초 갭다운 | Entry 가드 | fast_loop entry | future |
| G4 | 시초 추격 손절 timing | Exit timing | fast_loop exit | future |
| G5 | today_exit_cooldown | Entry 가드 (재진입) | Strategy Engine | DONE 5-15 |
| **G6** | **Thesis-aware exit** | **Hold 가드 (신설 축)** | **slow_loop 신규 + fast_loop forced_liquidation 확장 (Redis 키 분리)** | **Phase A 진입 (Pre-flight 2026-05-17 통과, catalog 8종)** |

핵심 직교성: G1~G5 는 모두 "entry 시점" 의사결정만 다룸. 보유 중 thesis 가
깨졌을 때의 hold 결정은 현재 **가격 rule (fixed_sl / trailing_tp / time_stop)
만으로 판단** — thesis 무관. G6 는 그 축에 thesis 의 의미적 평가를 추가.

## 2. 어제 사용자가 정리한 "slow loop blindness 4종" 의 확장

| # | slow loop 이 인식 못 한 것 | 해결 |
|---|---|---|
| 1 | 자기 행동의 결과 (자기 추천이 어떻게 끝났는가) | G1 ✓ |
| 2 | 자기 행동의 부산물 (오늘 청산했는가) | G5 ✓ |
| 3 | 시장의 현재 위치 (이미 너무 올랐는가) | G2 (design) |
| 4 | 시초 시점 상태 (어제 종가가 아닌 지금) | G3 (future) |
| **5** | **자기 thesis 의 현재 유효성 (산 이유가 아직 유효한가)** | **G6 (본 design)** |

## 3. 문제 정의 — 현재 thesis 의 무력

### 3.1 현재 영속화 — 자연어 1줄

`position_sheets.provenance.scout_hypothesis: str` (max 200자, 자연어).
예시 (5-13~5-15 003670 9회 반복 발행 시 사용된 hypothesis):

> "KOSPI 강세 + 반도체/IT 섹터 모멘텀(+36%) + 실적 호재 뉴스 + 리스크 이벤트 없음"

### 3.2 무력함

- **검증 불가능** — "KOSPI 강세" 가 깨졌는지 코드로 판정할 방법 없음
- **재평가 없음** — entry 후 hypothesis 가 어떻게 되든 hold 결정에 영향 0
- **exit 는 가격만** — fixed_sl / trailing_tp / time_stop. "thesis 가 깨졌으니 가격 무관 즉시 매도" path 없음

### 3.3 5-15 사고에 어떻게 작용했을 것인가

5-15 손절 10건의 entry 시점 hypothesis 는 "KOSPI 강세 + 반도체 모멘텀 + 실적 호재".
5-15 09:00 시초에 **KOSPI -4% CRITICAL** 로 전환됨. thesis 의 "KOSPI 강세"
조건은 09:00 시점에 명백히 깨짐. 그러나:
- 5-14 진입한 sheet 들은 그 정보 무관 보유 지속
- exit 는 fixed_sl 가 trigger 될 때까지 (평균 -5%) 대기
- thesis-aware exit 가 있었다면 09:00 KOSPI 시그널 깨짐 즉시 시장가 매도 → 손실폭 축소 가능

## 4. 핵심 설계 — Thesis 의 객체화

### 4.1 자연어 hypothesis 와 병행 — 검증 가능한 조건 list

```python
class ThesisCondition(BaseModel):
    """결정론 evaluator 로 평가 가능한 단일 조건."""
    type: Literal[
        "kospi_gate",              # macro_state.gate 평가
        "kospi_change_pct_above",  # KOSPI 등락률 임계
        "sector_momentum_above",   # 섹터 N일 누적 상승률
        "no_risk_event_high",      # 24h 내 high-impact risk_event 없음
        "earnings_event_window",   # earnings event 후 N영업일 이내
        "rsi_below",               # 1일봉 RSI 임계 미만 (MEAN_REVERT_RSI 용)
        "price_above_breakout",    # GAP_UP_REBOUND 의 직전 봉 high 돌파 유지
        "r20d_above_threshold",    # 종목 20영업일 누적 수익률 임계 이상 (Pre-flight 2026-05-17 추가)
    ]
    params: dict[str, Any]         # type 별 schema 검증

class ThesisSpec(BaseModel):
    """Scout 가 entry 시점에 생성, position_sheets.provenance 영속화."""
    natural_language: str  # 기존 scout_hypothesis (호환)
    conditions: list[ThesisCondition]
    critical_conditions: list[int]  # conditions index — 깨지면 즉시 invalidated
```

### 4.2 조건 catalog — 좁게 시작 (7종 초안)

| type | 의미 grain | params 예 | evaluator 입력 | 평가 비용 |
|---|---|---|---|---|
| `kospi_gate` | macro **종합 판정** (gate ∈ open/closed) | `{"required": "open"}` | macro_state | 0 (in-memory) |
| `kospi_change_pct_above` | KOSPI **단일 지표** (정량 threshold) | `{"min_pct": -0.02}` | KOSPI 현재가 | 1 KIS snapshot |
| `sector_momentum_above` | 섹터 N일 누적 모멘텀 | `{"sector": "semiconductor", "lookback_days": 5, "min_pct": 0.0}` | daily_prices 섹터 가중 | 1 SQL |
| `no_risk_event_high` | high-impact risk event 부재 | `{"hours": 24}` | news_events DB | 1 SQL |
| `earnings_event_window` | earnings event 후 N영업일 이내 | `{"max_days": 7}` | news_events DB | 1 SQL |
| `rsi_below` | 1일봉 RSI 임계 미만 (MEAN_REVERT_RSI 진입 신호) | `{"window": 14, "max": 30}` | daily_prices | 1 SQL + calc |
| `price_above_breakout` | 직전 봉 high 돌파 유지 (GAP_UP_REBOUND) | `{"reference_price": 12500}` | KIS snapshot | 1 KIS snapshot |
| `r20d_above_threshold` | 종목 20영업일 누적 수익률 유지 (Pre-flight 2026-05-17 추가, hypothesis "20일 모멘텀") | `{"min_pct": 0.0}` | daily_prices | 1 SQL |

**`kospi_gate` vs `kospi_change_pct_above` 의미 분리 (약점 #5 해결)**:
- `kospi_gate` 는 macro_state 의 **종합 판정** (gate, size_multiplier 등 종합) — 보수 신호. `closed` 면 매크로 council 이 위험 단정.
- `kospi_change_pct_above` 는 KOSPI **단일 지표 정량 threshold** — thesis 가 "KOSPI 1% 이상 강세 유지" 같은 강한 신호 표현용. gate 가 open 이어도 KOSPI 가 0.5% 만 오르면 thesis 약화 가능.
- 두 condition 은 grain 이 다름 (종합 vs 단일) — 중복 아님. catalog v1 양쪽 유지.

추가는 MINOR 업데이트. 7종의 5-15 hypothesis 표현률은 **Pre-flight 작업 (§8.5) 으로 사전 검증** — 가설로 두지 않음.

### 4.3 결정론 vs LLM — catalog 가 정공법인 이유

대안: 자연어 hypothesis + 현재 시장 상태를 LLM 에게 줘서 "still valid?" 판단.

기각 이유 (`feedback_prompt_control_limit` 원칙):
- exit 결정은 결정론 layer 여야 함 (LLM 비결정성 → invalidated 판정 변동성 → 매매 일관성 0)
- 비용 (sheet 보유 평균 30~50개 × 1시간 cron × DeepSeek 호출)
- 책임 추적 불가 (왜 5-15 11:00 에 invalidated 됐는가 — LLM 답 재현 불가)

catalog 의 trade-off: 표현력 한계. 단, **잘 정의된 7종이 모호한 LLM 평가 한 번보다 안전**. handoff 의 GAP_UP_REBOUND `price_above` 자동부착 처럼, 결정론 catalog 가 정확히 우리 시스템 패턴.

### 4.4 critical_conditions 선정 — LLM × policy intersection

Scout LLM 이 conditions list 와 함께 critical_conditions index 를 지정. 그러나
LLM 의 critical 선정 일관성 신뢰 X (`feedback_prompt_control_limit`) — 보수적
LLM 이 모든 condition critical 처리하면 invalidated 폭주, 과격적 LLM 이 critical 0
지정하면 G6 무력.

→ **policy 가 strategy_tag 별 critical 후보 catalog 을 강제**. 최종 critical =
LLM 지정 ∩ policy 후보.

| strategy_tag | critical 후보 (policy v1) |
|---|---|
| GAP_UP_REBOUND | `price_above_breakout`, `kospi_gate` |
| SECTOR_MOMENTUM | `sector_momentum_above`, `kospi_gate`, `r20d_above_threshold` |
| EARNINGS_DRIFT | `earnings_event_window`, `no_risk_event_high` |
| MEAN_REVERT_RSI | `rsi_below` |

`r20d_above_threshold` 는 SECTOR_MOMENTUM 에만 critical 후보 — 모멘텀 strategy
의 본질 ("20일 모멘텀 유지") 깨짐이 thesis 무효 직접 신호. EARNINGS_DRIFT 는
earnings 의존이라 r20d 는 informational (critical 후보 X).

규칙:
1. LLM 이 critical 로 지정한 condition 중 policy 후보에 속한 것만 최종 critical 인정
2. LLM critical 지정 0 → policy 후보 중 LLM 이 conditions 에 포함한 첫째를 자동 critical (1개 보장, 빈 critical 차단)
3. LLM critical 지정이 policy 후보 외 (off-target) → demote (일반 condition 처리, 무시 X)

Phase B 측정: LLM critical 지정의 policy 후보 매칭률 (target > 80%). < 50% 시
policy 만으로 갈지 검토 (LLM 의 critical 권한 회수).

근거: 약점 #6 — LLM 위임의 위험을 policy intersection 으로 결정론적 안전망 + LLM
의도 일부 존중 (보수적 hybrid).

## 5. 재평가 layer 위치

### 5.1 slow_loop 신규 함수 — `thesis_revaluation`

`prime_jennie_runtime/slow_loop/thesis/revaluator.py` (신규 모듈):

```python
class ThesisRevaluator:
    """보유 position_sheets 의 ThesisSpec 을 정기 재평가.
    
    Cron: 1시간 (장중 09:30 ~ 15:00, 6 tick). 장 마감 외엔 발화 안 함.
    출력: 4-state 분류 + invalidated ticker → forced_liquidation Redis 키 적재.
    """
    
    async def revaluate_all_held(self) -> ThesisRevaluationReport:
        held = await self._fetch_held_sheets()  # position_sheets × executions
        results = []
        for sheet in held:
            state = await self._evaluate(sheet.thesis_spec)
            results.append((sheet, state))
        invalidated = [s for s, st in results if st == "invalidated"]
        if invalidated:
            await self._publish_forced_liquidation([s.ticker for s in invalidated])
        return ThesisRevaluationReport(results=results)
```

### 5.2 4-state 분류 + 후속 액션

| state | 판정 | 후속 |
|---|---|---|
| `valid` | 모든 conditions True | (no-op, log debug) |
| `strengthened` | conditions 강화 시그널 (별도 SECONDARY catalog) | log info, **본 v1 에선 액션 없음** (piramiding 은 future) |
| `weakened` | non-critical conditions 일부 False | log warning, **telegram 즉시 알림 X — 15:30 EOD summary 일괄** (§5.6) |
| `invalidated` | critical_conditions 중 하나라도 False | **`forced_liquidation:thesis` Redis SET 에 ticker 적재 → fast_loop 즉시 시장가 매도** (§5.3) |

### 5.3 통신 path — Redis 키 분리 + fast_loop 소량 확장

기존 forced_liquidation 은 `tick_loop.py:183` 에서 reason 이 하드코딩:
```python
decision = ExitDecision(should_close=True, reason="forced_liquidation", portion=1.0)
```

→ "fast_loop 변경 0 + 재사용" 은 불가능 (reason 분기 필요). 정직하게 **Redis 키
분리 + fast_loop 소량 확장** 로 수정.

**Redis 키 layout**:
- `forced_liquidation:user` — telegram 봇 명령 (기존 `forced_liquidation:stocks` 에서
  rename, 의미 명확화)
- `forced_liquidation:thesis` — G6 thesis_revaluator 가 적재 (신규)

**fast_loop 변경** (`tick_loop.py:_evaluate_forced_liquidation`):
- 두 SET 을 SUNION 으로 한 번에 조회 → 우선순위로 reason 결정
  - user > thesis (사용자 명시 명령이 우선)
- decision 의 reason 을 `forced_liquidation:{source}` 로 설정
  - user SET hit → `forced_liquidation:user`
  - thesis SET hit only → `forced_liquidation:thesis`

코드 변경 규모: tick_loop 30~50 LOC + telegram_bot/control.py 의 KEY 상수 rename.
하위 호환 위해 1 release 는 `forced_liquidation:stocks` 도 read 병행 (rename 안전망).

**outcome 추적**: `executions.metadata_json->>'exit_reason'` 에 `forced_liquidation:user`
/ `forced_liquidation:thesis` 가 그대로 저장 → cooldown 가드 SQL 의 `STOP_REASONS`
포함 여부 결정 + G6 발화 정량 추적 모두 가능.

**cooldown 가드 영향**: 현재 `STOP_REASONS = ("fixed_sl", "stop_loss", "breakeven_stop")`.
G6 의 `forced_liquidation:thesis` 는 STOP_REASONS 미포함 → 24h cooldown 안 걸림.
의도된 동작: thesis invalidation 은 가격 손절과 다른 의미 (외부 조건 변화), cooldown
까지 강제하면 회복 시 재진입 차단 부작용. Phase B 측정 후 STOP_REASONS 편입 검토.

### 5.4 컨테이너 / 프로세스

- slow_loop 컨테이너 내 신규 모듈 + cron 추가 (1시간 tick)
- fast_loop `tick_loop.py` 의 `_evaluate_forced_liquidation` 소량 확장 (§5.3)
- telegram_bot `control.py` 의 KEY 상수 rename + 하위 호환 read 1 release
- 새 컨테이너 X
- 새 Redis 키 1개 (`forced_liquidation:thesis`), 기존 키 1개 rename
- 새 DB 테이블 X (thesis_spec 은 `position_sheets.provenance_json->'thesis_spec'` 에 nested 영속)

### 5.5 Fail 정책 (layer 별)

enforcement layer 의 fail-open 은 가드 무력화 위험 (§11) — layer 별 정책 명시:

| Layer | 실패 상황 | 정책 | 근거 |
|---|---|---|---|
| (A) thesis_spec 부재 | sheet 의 provenance.thesis_spec 이 None (점진 도입기 / 호환) | **skip (no-op, log debug)** | Phase A 호환. 운영 1주 측정 후 미반환률로 prompt 강화 판단. |
| (B) condition evaluator 실패 | DB 장애 / KIS snapshot timeout / SQL 오류 | **`eval_failed` 메타 상태 + telegram 알림 + 매도 X** (fail-loud) | 평가 불가 condition 을 invalidated 도 valid 도 단정 불가. 사람 개입. |
| (C) revaluator 자체 장애 | slow_loop 죽음 / cron 미발화 | **heartbeat 알림** (tick_loop 패턴) + 다음 cron 까지 대기. 자동 복구 없으면 telegram 알림 | 가드 미작동 자체를 빨리 인지. |
| (D) 모든 critical_conditions 평가 성공 + 일부 False | 정상 평가 | **invalidated → forced_liquidation:thesis 적재** | 본 design 핵심 동작 |
| (E) 모든 conditions 평가 성공 + 모두 True | 정상 평가 | valid (no-op) | 본 design 정상 path |

**(B) `eval_failed` 가 4-state 옆의 메타 상태인 이유**: 4-state (valid/strengthened/
weakened/invalidated) 는 *의사결정 가능* 분류. eval_failed 는 분류 불가 → 별도 처리.
4-state 표는 그대로 유지, eval_failed 는 평가 결과의 메타 attr 로.

**알림 형식 (B/C)**:
- (B) `[G6] eval_failed: ticker=003670 condition_idx=2 (sector_momentum_above) — DB timeout`
- (C) `[G6] revaluator heartbeat lost — last_run=12:00, current=14:30 (2.5h gap)`

(B) 알림은 같은 ticker × condition 묶음 30분 dedup.

### 5.6 알림 정책 (state 별)

약점 #7 (weakened 알림 폭주) 해결 — state 별 알림 / dedup 정책 명시:

| state / event | telegram 알림 | 로그 level | dedup 정책 |
|---|---|---|---|
| valid | X | debug | - |
| strengthened | X | info | - |
| weakened | **X (1일 1회 summary 일괄)** | warning | 1일 (15:30 EOD summary 1회) |
| invalidated | **즉시** (매도 action 동반) | warning | per-ticker, 1 cron tick |
| eval_failed (§5.5 B) | 30분 dedup | error | 30분 per (ticker × condition) |
| revaluator heartbeat (§5.5 C) | 즉시 | error | per-incident (자동 dedup) |

핵심:
- **weakened 는 즉시 알림 안 함** — 보유 30 sheet × 시장 약세 day = 알림 가시성 0.
  대신 15:30 EOD summary 1회 (`"오늘 weakened: 8/30, 주요 패턴: kospi_change_pct_above"`).
- **invalidated 만 즉시 알림** — 매도 action 트리거이므로 사용자 인지 필수.
- eval_failed dedup 30분 — DB 장애 burst 시 같은 메시지 폭주 회피.

## 6. Scout 통합

### 6.1 Scout output schema 변경

`ScoutOutput.screening_code` 가 생성하는 Python 함수가 `ScreeningCandidate`
에 `thesis_spec: ThesisSpec` 도 채워서 반환. 현재 자연어 `notes` 옆.

`prompts.py` v0.7 → v0.8 — system prompt 에 catalog 7종 명세 + 예시 추가:

```
hypothesis 자연어 1줄과 함께, 검증 가능한 thesis_conditions 를 반환할 것.
catalog (7 type) 외 사용 금지. critical_conditions index 는 깨지면 즉시
매도 트리거 — 보수적으로 선정 (보통 kospi_gate / sector_momentum_above
중 1~2개).
```

### 6.2 점진 도입 — Scout 가 thesis_spec 미반환 시 fallback

기존 시트 호환 + Scout LLM 학습 기간 동안 `thesis_spec` 비어있을 수 있음 →
`ThesisRevaluator` 는 thesis_spec 없는 sheet 는 skip (fail-open, 기존 동작 유지).

운영 1주 후 미반환 비율 측정 → 80% 이상 반환되면 prompt 강화 / required 화 검토.

## 7. Strategy Engine 통합

`build_sheet_with_reason` 에 thesis_spec 통과 path 추가:
- candidate.thesis_spec 이 있으면 `ProvenanceSection.thesis_spec` 으로 영속
- 없으면 None (호환)

기존 G1~G5 가드 로직 변경 0. **본 design 은 entry 단계 비건드림** — hold/exit 만.

## 8. Tests

### 8.1 Unit — condition evaluator

`tests/slow_loop/thesis/test_evaluators.py`:
- 각 type 별 True/False 케이스
- params 누락 / catalog 외 type → evaluator skip (condition True 처리, §5.5 A 와 별개 — schema-level)
- DB 장애 / KIS timeout → **`eval_failed` 메타 상태 반환** (§5.5 B 와 일치. 매도 안 함)
- revaluator 통합 test 에서 `eval_failed` 가 invalidated 로 잘못 분류되지 않음 검증

### 8.2 Unit — revaluator 분류

`tests/slow_loop/thesis/test_revaluator.py`:
- critical 깨지면 invalidated
- non-critical 깨지면 weakened
- 모두 True 면 valid
- thesis_spec 없으면 skip (no-op)

### 8.3 Integration

`tests/integration/test_thesis_revaluation_to_forced_liquidation.py`:
- invalidated 판정 → Redis SET 에 ticker 적재 확인
- fast_loop 가 그 SET 보고 매도 (mock executor) → metadata reason 검증

### 8.4 Backtest 시나리오 (도입 전 필수)

5-15 손절 10종목의 5-14 ~ 5-15 데이터로 **LLM critical 선정 분포 3종 시뮬레이션**
(약점 #4 해결 — 단일 가정 leap 회피):

| 시나리오 | 가정 | 추정 invalidated 비율 | 추정 평균 손실폭 |
|---|---|---|---|
| **보수** | LLM critical 0 → policy auto-critical 1개 (§4.4 규칙 2). EARNINGS_DRIFT 는 `earnings_event_window`, SECTOR_MOMENTUM 은 `sector_momentum_above` 등 strategy_tag 별 첫째 후보. KOSPI 단일 시그널만으론 invalidated 못 잡음. | 4~6/10 | -3.5% ~ -4.0% |
| **표준** | LLM 이 strategy_tag 의 policy 후보 1~2개 정확 지정 (kospi_gate + sector_momentum_above 등). 5-15 09:00 매크로 깨짐 + 섹터 약세 동시 catch | 8~9/10 | -2.5% ~ -3.0% |
| **낙관** | LLM 이 conditions 4~5개 채우고 그중 critical 2~3개 정확 지정 + policy 매칭 100% | 10/10 | -1.5% ~ -2.0% |

**측정 절차**:
1. 각 시나리오로 5-15 ticker 별 thesis_spec 생성 (수동)
2. 5-15 09:00 / 09:30 / 10:00 / 10:30 시점에 revaluator 시뮬레이션
3. 첫 invalidated tick 의 KOSPI 종목 price 로 매도 가정 → 손실폭 계산
4. baseline (G6 없음, 실제 fixed_sl 평균 -5%) 와 비교

**Phase B 판정 기준**: **보수 시나리오 의 EV 가 baseline 보다 유의미하게 우월**
(즉, LLM 이 최악으로 critical 선정해도 G6 가 손실 축소). 표준/낙관은 upside 추정.

가정 검증: Phase A 1주 후 LLM critical 분포 실측 → 3종 시나리오 비중 재조정.

## 8.5 Pre-flight (Phase A 진입 전 1회 작업)

> **✅ 2026-05-17 실행 완료**. 결과:
> [`.ai/analyses/2026-05-17-g6-hypothesis-catalog-coverage.md`](../analyses/2026-05-17-g6-hypothesis-catalog-coverage.md)
> — **통과 (catalog 7→8종, `r20d_above_threshold` 추가)**. paragraph 분석 추정,
> Phase A 1주 후 LLM 의 실 thesis_spec 반환률로 실증.

약점 #3 (catalog 7종 표현률 가설) 해결 — Phase A 진입 전 실증.

**작업 절차** (예상 2~3시간):

1. **historical hypothesis 추출** (지난 30일):
   ```sql
   SELECT generated_at::date AS d, ticker, strategy_tag,
          provenance_json->>'scout_hypothesis' AS hypothesis
   FROM position_sheets
   WHERE generated_at > now() - interval '30 days'
   ORDER BY generated_at DESC;
   ```
   예상 row 50~100건.

2. **매핑 시도 (수동)**:
   - 각 hypothesis 를 §4.2 catalog 7종으로 매핑 시도
   - 매핑 가능 → 어떤 type 조합 / critical 후보
   - 매핑 불가 → 어떤 의미 표현 catalog 부족

3. **매핑률 측정**:
   - target ≥ 80%
   - 미달 시 catalog 확장 (8~10종) → 재매핑 → 측정 반복
   - 매핑 가능한 hypothesis 비율이 Phase A 진입 gating

4. **catalog v1 확정**:
   - 매핑률 ≥ 80% 인 catalog 으로 Phase A 진입
   - 미달분 hypothesis 패턴은 Open Question 으로 기록 (`unmappable_hypotheses.md`)

**산출물**: `.ai/analyses/2026-05-XX-thesis-catalog-coverage.md` — 매핑률 + 각 hypothesis
× catalog type 매핑 표 + 미매핑 패턴 분석.

본 작업 완료 후 Phase A schema 작업 착수.

## 9. Rollout

| 단계 | 시기 | 내용 |
|---|---|---|
| Phase A | 5-22 (금) 이후 | thesis_spec schema + Scout prompt v0.8 + 영속화 (재평가는 안 함, 데이터만 누적) |
| Phase B | 6-01 ~ 6-07 | ThesisRevaluator 도입, advisory mode (log + telegram, **forced_liquidation 적재 X**) |
| Phase C | 6-08 ~ | invalidated 시 forced_liquidation 적재 enforce. 첫 1주 정량 측정. |

장중 deploy 금지 룰 준수 — Phase 전환은 모두 일요일 또는 평일 15:30 이후.

advisory → enforce 점진 도입은 Coordinator design (5-14) 의 §0 원칙과 동일.

## 10. 측정 지표

| 지표 | 측정 방법 |
|---|---|
| Scout thesis_spec 반환율 | screening_candidates 중 `provenance_json->>'thesis_spec' IS NOT NULL` 비율 |
| critical conditions 평균 개수 | thesis_spec.critical_conditions length 분포 |
| 각 state 발화 빈도 | thesis_revaluation_reports (신규 테이블 또는 log) |
| G6 forced_liquidation 발화 후 사후 가격 | 매도 후 1~5일 가격 추이 — 너무 빨리 매도했는가 측정 |
| 5-15 패턴 재현 시 G6 효과 | backtest §8.4 결과 |

## 11. 위험 / 트레이드오프

| 위험 | 완화 |
|---|---|
| catalog 7종 부족으로 hypothesis 표현 한계 → conditions 빈약 | Phase B advisory mode 1주 측정 후 catalog 확장 (8~12종) |
| Scout LLM 이 critical 조건을 너무 많이 / 너무 적게 선정 | prompt v0.8 가이드 + 평균 critical 개수 모니터링 (target: 1~3개) |
| invalidated 판정 후 실제로는 회복 종목 매도 (over-blocking) | sufficient_score 측정 (§10 4행) → 임계 완화 검토 |
| catalog evaluator 의 DB 부하 | 1시간 cron + sheet 평균 < 30개. 각 evaluator 평균 5ms × 30 × 7 = 1초/cron — 안전 |
| forced_liquidation reason 혼용 | Redis 키 분리 (`:user` / `:thesis`) + fast_loop tick_loop 의 reason 분기 (§5.3). outcome 분석에서 G6 발화 정량 추적. |
| ThesisRevaluator 가 죽으면 hold sheets 의 thesis 영구 미평가 | §5.5 (C) heartbeat 알림 — fail-loud. 매도는 보수적으로 X. |
| condition evaluator 의 DB 장애 → fail-open 시 invalidated 검출 0 → 가드 무력 | §5.5 (B) `eval_failed` 메타 상태 + telegram 알림 + 매도 X. 사람 개입 트리거. |

## 12. 민지 문서에서 의도적으로 제외한 것

| 민지 §X | 제외 이유 |
|---|---|
| 2단 포트폴리오 thesis (섹터 노출, 상관, 베타) | 현재 운영 portfolio 동시 1~3 종목. 종목 < 5 면 섹터 상한/상관 의미 약함. Phase 0 #1 후 매매 빈도 증가 시 재검토. |
| 3단 자산배분 5단계 (NORMAL/CAUTION/...) | 우리 `macro_state.gate + size_multiplier` 가 이미 함수형. discrete level 강제는 표현력 잃음. |
| "확신 변수 금지" | Phase 0 #1 (conviction-outcome correlation) 에서 효용 검증 예정. 무조건 폐기 X. |
| 5거래일 cooldown + 영구 차단 + 사람 review 큐 | 우리 G5 (same-day) + 24h cooldown 으로 충분. 5거래일은 매매 거의 차단. 사람 review 큐는 우리 아키텍처 외. |
| "shadow 모드 5거래일" | manual trigger + 직접 호출 검증 패턴 (5-15-0003 학습) 로 충분. |

민지 문서의 "지향점" 만 취해서 우리 시스템 모양으로 녹인 결과 = **본 design**.

## 13. Open Questions

해결된 항목 (design v2 본문 통합):
- ~~catalog 80% 표현률~~ → §8.5 Pre-flight 작업으로 사전 검증
- ~~critical_conditions 선정 권한~~ → §4.4 LLM × policy intersection
- ~~weakened 알림 폭주~~ → §5.6 1일 1회 EOD summary

남은 Open:
- **strengthened state 의 활용** (피라미딩) — 별도 G7 후보. 본 design 범위 외.
- **thesis_revaluation cron 주기 1시간이 적절한가** — 5-15 케이스는 09:00 시초 즉시 깨짐 → 09:30 evaluation 까지 30분 lag. 5분 단위로 줄이는 비용 vs 가치. Phase B 측정 (5-15 같은 시초 critical event 빈도) 후 결정.
- **conditions evaluator 의 cache 정책** — 같은 cron tick 내 KOSPI snapshot / macro_state 등 공통 fetch 는 1회 prefetch. 구현 단계에서 결정.
- **`forced_liquidation:thesis` 의 STOP_REASONS 편입 여부** — §5.3 후속. thesis invalidation 후 cooldown 도 걸지 결정. Phase B 측정 후.
- **LLM critical 지정의 policy 후보 매칭률 < 50% 시 LLM 권한 회수 여부** — §4.4 후속. Phase B 데이터 후.

## 14. 참조

- 본 design 자매: `.ai/designs/2026-05-17-g2-overextension-validator.md`
- 5-layer 통합 design: `.ai/designs/2026-05-15-scout-overextension-guards.md`
- Coordinator design: `.ai/designs/2026-05-14-agent-coordinator.md` (§0 advisory→enforce, Decision Authority Layer B)
- session 핸드오프: `.ai/sessions/session-2026-05-15-0003.md`
- 코드 위치:
  - `prime_jennie_runtime/slow_loop/thesis/` (신규 모듈)
  - `prime_jennie_runtime/slow_loop/scout/schemas.py` (ThesisSpec 추가)
  - `prime_jennie_runtime/slow_loop/scout/prompts.py` (v0.8 catalog 가이드)
  - `prime_jennie_runtime/position_sheet/schema.py` (`ProvenanceSection.thesis_spec`)
  - `prime_jennie_runtime/fast_loop/tick_loop.py:163` (`_evaluate_forced_liquidation` 소량 확장 — 두 SET SUNION + reason 분기, §5.3)
  - `prime_jennie_runtime/telegram_bot/control.py:73` (KEY 상수 rename `:stocks` → `:user`, 1 release 하위 호환 read)
- 글로벌 메모리:
  - `feedback_prompt_control_limit` (LLM 결정 제어 X, 결정론 layer 에서)
  - `project_v3_orchestrator_decision` (Coordinator + Harness 패턴)
  - `project_vision_llm_autonomy_dropped` (Phase 0 선결과제 #1 = G1 데이터로 진척)
