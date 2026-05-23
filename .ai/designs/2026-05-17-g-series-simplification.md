# G 시리즈 단순화 결정 (2026-05-17)

> **2026-05-23 갱신**: 5-22 결정론 코어 전환 후 **부분 archive / 부분 재설계**. outcome_feedback (구 G1) 의 Scout context 노출은 의미 변질, same_day_cooldown (구 G5) 은 도입 완료, thesis_aware_hold (구 G6) 는 출처 끊김으로 재설계 필요. 자세한 분류와 thesis 줄기의 세 갈래 (A/B/C) — `.ai/designs/2026-05-23-post-llm-at-core-realignment.md` §8.2, §9.

> 1주일 (5-10 ~ 5-17) 만에 G1~G6 명명 + 두 design v3.1 + Pre-flight + Phase
> A/B/C + catalog 8종 + 4-state + fail 정책 5종 등 doc 비대화. **doc 의 정밀도가
> 운영의 정밀도를 앞지름** — 본 doc 으로 재정립 + 단순화.

## 1. 원래 추구하던 방향 — 한 문장

> "slow_loop 이 정보 결손으로 같은 손실 패턴 반복, 그리고 hold/exit 결정이
> 시장 상태 변화 무관" — 5-15 사고의 진짜 한 문장 진단.

여기서 결정해야 할 건 **3 가지**:

| # | 결정 | 답 |
|---|---|---|
| A | Slow loop 이 자기 결과 / 자기 부산물 인식? | `outcome_feedback` (DONE 5-15) + `same_day_cooldown` (DONE 5-15) |
| B | Hold/exit 결정이 시장 / thesis 변화 반응? | `thesis_aware_hold` (Phase A 도입 5-17) — **진짜 메인** |
| C | 시초 5분 특수성 처리? | 별개 트랙 (B 와 직교) — backlog |

## 2. 명명 정리 — G 시리즈 폐기, 의미 기반 3 카테고리

```
[Awareness]    outcome_feedback        — Scout context: scout_outcomes_v1 view + previous_outcomes
[Cooldown]     same_day_cooldown       — Strategy Engine: today_exit_cooldown
[Hold-thesis]  thesis_aware_hold       — Scout thesis_spec + slow_loop revaluator

[Backlog 트랙] open_5min_gap_block / open_5min_hold (시초 timing)
[Deprecated]   overextension_entry_guard (구 G2 — Pre-flight 부정, 폐기)
```

기존 G1~G6 명명은 backwards compat 위해 본 doc 와 commit message 에서만 인용.
신규 design / 메모리 / handoff 는 의미 기반 명명 사용.

## 3. 폐기 — `overextension_entry_guard` (구 G2)

**근거**: 5-17 Pre-flight 결과 임계값이 손익 구분 못함.
- 익절 차단률 80% > 손절 차단률 70% — 차단할수록 손해
- SECTOR_MOMENTUM 익절 차단 85.7% — 모멘텀 strategy paradox
- 가격 기반 지표 (close_to_high_60d / r20d / r5d) 가 outcome 예측력 없음

**hold 측면 흡수**: G6 catalog 의 `sector_momentum_above` / `kospi_gate` / `r20d_above_threshold` 가 이미 hold 측면에서 동일 지표 활용. 별도 entry 가드 불필요.

**entry 측면**: 모멘텀 약화 지표 (RSI div / MACD turning / 거래량 감소) 후보는 future backlog 1줄로 보관, 별도 design X.

기존 doc: `.ai/designs/2026-05-17-g2-overextension-validator.md` — status "Deprecated" 로 갱신, archive 유지.
Pre-flight 분석: `.ai/analyses/2026-05-17-g2-thresholds-historical-validation.md` — 학습 산출물로 보관.

## 4. `thesis_aware_hold` (구 G6) 단순화

### 4.1 catalog 8 → 5종

| 유지 (Phase A 측정 후 확장 가능) | 제거 (Phase A 데이터 후 재검토) |
|---|---|
| `kospi_gate` | `kospi_change_pct_above` (gate 와 grain 중복 — Phase A 후 분리 필요 시 추가) |
| `sector_momentum_above` | `price_above_breakout` (GAP_UP_REBOUND 만 의존, 빈도 낮음) |
| `no_risk_event_high` | `r20d_above_threshold` (G2 부정 결과 영향, Phase A 데이터 후 재검토) |
| `earnings_event_window` | |
| `rsi_below` | |

근거: Pre-flight 의 hypothesis 빈도 분석에서 5종이 "거의 전부" 표현. 나머지 3종은 사변. Phase A 1주 LLM 반환 패턴 보고 확장.

### 4.2 Phase A/B/C → 2 단계

| 신규 단계 | 시기 | 내용 |
|---|---|---|
| **Phase 1** (= 구 A+B) | 5-22 ~ 5-29 (1주) | schema 영속 + ThesisRevaluator advisory mode (log only, 매도 X) |
| **Phase 2** (= 구 C) | 5-29 ~ | enforce — invalidated → forced_liquidation:thesis Redis SET |

6-08 → 5-29 약 1주 단축.

### 4.3 4-state → 2-state (Phase 1 v1)

| 신규 state | 의미 |
|---|---|
| `valid` | critical_conditions 모두 True 또는 conditions 비어있음 |
| `invalidated` | critical_conditions 하나 이상 False |

`strengthened` / `weakened` 분류는 Phase 1 측정 후 정량 가치 보고 도입 결정. Phase 1 은 단순 2-state 로 시작.

### 4.4 critical 선정 — policy-only (LLM 자유 지정 제거)

| 신규 (단순) | 기존 (복잡) |
|---|---|
| policy 가 strategy_tag 별 critical 후보 catalog 강제. LLM 의 critical_conditions 자유 지정 없음. | LLM × policy intersection — LLM 지정 ∩ policy 후보 |

근거: Phase 1 측정 전 LLM 의 critical 선정 신뢰성 알 수 없음. policy-only 가 결정론. LLM 의 critical 자유 지정은 측정 후 가치 있으면 도입.

policy critical (strategy_tag 별):
- GAP_UP_REBOUND: `kospi_gate` (sector breakout 후속 측정 필요)
- SECTOR_MOMENTUM: `kospi_gate`, `sector_momentum_above`
- EARNINGS_DRIFT: `earnings_event_window`, `no_risk_event_high`
- MEAN_REVERT_RSI: `rsi_below`

### 4.5 Fail 정책 5종 → 2종

| 신규 (단순) | 의미 |
|---|---|
| **skip** (정상 fail-open) | row 없음 / catalog 외 type / params 누락 → condition True 처리 |
| **alert + skip** (DB 장애) | DB 장애 / timeout → telegram + condition True (보수) |

`eval_failed` 메타 상태 제거. 운영 측정 후 정밀화 필요 시 추가.

## 5. design doc 룰 — v1 + Pre-flight + commit

| 변경 규모 | doc 패턴 |
|---|---|
| 신규 layer (예: thesis-aware hold) | **v1 + Pre-flight + commit**. 100~150 lines. |
| 작은 변경 / 보강 | doc 없이 commit message 로 |
| Read-through 정정 cycle | **금지** — 본 simplification 까지 학습 |

self-critique 는 사용자 요청 시만. doc 의 본질 = 결정, 정밀도 아님.

## 6. 즉시 정리 작업

1. `.ai/designs/2026-05-17-g2-overextension-validator.md` — status header "Deprecated" 명시 + 본 doc cross-ref. 본문 archive 유지.
2. `.ai/designs/2026-05-17-g6-thesis-aware-exit.md` — status header "Simplified, see 2026-05-17-g-series-simplification.md" + Phase 1/2 통합 명시.
3. `~/.claude/global-memory-youngs7596/trading-domain.md` — G 시리즈 표 → 3 카테고리.
4. 로컬 memory `feedback_design_doc_simplicity` 신규.

코드 변경 없음 — Phase A 영속 + Scout prompt v0.8 그대로 운영. Phase 1 의 advisory revaluator 도입은 5-22 이후.

## 7. 남은 미해결 (별개 트랙 — 본 doc 외)

- **시초 timing 트랙**: gap_down_block / open_5min_hold — design 없음, backlog
- **Phase 0 #1** conviction-outcome correlation — G1 데이터 누적 후
- **Phase 0 #2** 5% SL 진단 — open_5min_hold 의 근거
- **Phase 0 #3** Macro calibration — 일요일 trigger_watcher 발화 같은 weekend 가드 부재

위 항목은 본 simplification 와 무관, 별도 시점에 결정.

## 8. 학습 (`feedback_design_doc_simplicity` 메모리화)

- 1주일에 6 layer 명명 + Phase A/B/C + 4-state + fail 5종 + LLM × policy intersection = doc 비대화
- design doc 의 self-critique cycle (v1 → v3.1) 이 정밀도는 높였지만 운영 단순성 잃음
- **결정해야 할 것은 비교적 간단** — 5-15 사고의 3 가지 결정 (awareness / cooldown / hold-thesis). 나머지는 noise
- 의미 기반 명명 > 순서 명명 (G 시리즈 inflation)
- v1 + Pre-flight + commit 패턴이 정공법

## 9. 참조

- 단순화 직전 doc:
  - G6 v3.1: `.ai/designs/2026-05-17-g6-thesis-aware-exit.md` (Phase 1/2 통합 명시 갱신 예정)
  - G2 v3.1: `.ai/designs/2026-05-17-g2-overextension-validator.md` (Deprecated 갱신 예정)
- Pre-flight 산출물 (학습 보관):
  - `.ai/analyses/2026-05-17-g2-thresholds-historical-validation.md`
  - `.ai/analyses/2026-05-17-g6-hypothesis-catalog-coverage.md`
- handoff: `.ai/sessions/session-2026-05-17-0001.md`
- 글로벌 메모리: `trading-domain.md` (3 카테고리 표 갱신 예정)
