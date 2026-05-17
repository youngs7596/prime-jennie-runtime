# G2 Overextension Thresholds — Historical Validation (Pre-flight)

**작업 일자**: 2026-05-17 (일)
**작업 시간**: ~1시간 (DB 쿼리)
**Design**: `.ai/designs/2026-05-17-g2-overextension-validator.md` §7.4
**결정**: **G2 구현 보류** — 임계값이 손익 구분 못함, 익절 차단률이 손절보다 높음.

---

## 1. 작업 목적

design §7.4 명시: 5-15 단일 day (KOSPI -4% CRITICAL special) 데이터로 임계값
잡은 게 over-fitting 위험 → 평상 day 손절 sample 추가 검증으로 over-blocking
사전 확인. **평상 day 차단률 > 50% 시 도입 보류**.

추가로: 손절 sample 만 보면 bias 있음 (이미 손실 본 종목) → **익절 sample
차단률 과 비교** 해서 G2 의 손익 구분 능력 fair 평가.

## 2. 데이터

- 기간: 2026-04-17 ~ 2026-05-15 (지난 30일)
- 손절 sample: 66건 (DISTINCT ticker × entry_day × strategy_tag)
- 익절/기타 sample: 30건
- 출처: position_sheets × executions (side=sell)
- 매크로 분포: 5-06 (36 stop), 5-10 (6), 5-11 (5), 5-13 (5), 5-14 (10), 5-15 (4)

## 3. 적용 임계값 (design §4.1 + §4.2)

| strategy_tag | close_to_high_60d | r20d | r5d |
|---|---|---|---|
| EARNINGS_DRIFT | > 0.90 | > 0.20 | > 0.15 |
| 기타 (default) | > 0.95 | > 0.25 | > 0.20 |
| MEAN_REVERT_RSI | (skip) | (skip) | (skip) |

세 룰 OR. MEAN_REVERT_RSI 는 본 가드 적용 제외 (design §5).

## 4. 결과 — 핵심 발견

### 4.1 전체 outcome 별 차단률

| outcome | n | blocked | block_pct |
|---|---|---|---|
| **stop (손절)** | 66 | 46 | **69.7%** |
| **profit_or_other (익절+기타)** | 30 | 24 | **80.0%** |

**익절 차단률 (80.0%) > 손절 차단률 (69.7%)**.

→ G2 는 손절보다 익절을 더 많이 차단. **차단할수록 손해** (자연 수익 기회를 더 차단).

### 4.2 strategy_tag × outcome 분포

| strategy_tag | stop n | stop block% | profit n | profit block% | 가치 |
|---|---|---|---|---|---|
| SECTOR_MOMENTUM | 37 | 64.9% | 21 | **85.7%** | **negative** (sample 58, 전체의 60%) |
| EARNINGS_DRIFT | 20 | 80.0% | 5 | 80.0% | **0** (구분 능력 없음) |
| GAP_UP_REBOUND | 7 | 85.7% | 4 | 50.0% | positive (sample 11, 너무 적음) |
| MEAN_REVERT_RSI | 2 | (skip) | 0 | (skip) | n/a |

**해석**:
- **SECTOR_MOMENTUM (최대 sample)**: 익절 차단률 85.7% vs 손절 64.9%. **G2 가 모멘텀 종목의 자연 수익 기회를 더 차단** — 모멘텀 strategy 의 본질 ("이미 오른 종목 따라감") 과 G2 임계값 ("이미 오른 종목 차단") 이 정면 충돌.
- **EARNINGS_DRIFT**: 80% / 80% 동일. 차등 임계값 (더 엄격) 적용에도 손익 구분 못함. design §4.2 의 EARNINGS_DRIFT 차등 근거 (87.5% 손절율) 가 G2 로 해결 불가 — strategy 자체 폐기 검토 별도 필요.
- **GAP_UP_REBOUND**: 86% / 50%. 손절 더 차단 — 유의미한 정 효과. 단 sample 11 (양쪽 합) 으로 통계 의미 부족.

### 4.3 day 별 차단률

| entry_day | sample | block_pct | 특이사항 |
|---|---|---|---|
| 2026-05-06 | 36 | 61.1% | 평상 day, 60% 초과 |
| 2026-05-10 | 6 | 66.7% | 평상 day |
| 2026-05-11 | 5 | 100% | 평상 day (sample 적음) |
| 2026-05-13 | 5 | 80% | 평상 day |
| 2026-05-14 | 10 | 70% | 5-15 사고 직전 |
| 2026-05-15 | 4 | 100% | special day (KOSPI -4%) |

평상 day 도 60~80% — design §7.4 "평상 day 차단률 < 30%" target 크게 초과.

## 5. 근본 원인 분석

### 5.1 임계값 over-fitting (5-15 단일 사건)

5-15 손절 10건의 8/10 차단을 보고 잡은 임계값 (60d 95% / 20d 25% / 5d 20%).
그러나 평상 day 의 모멘텀 종목 대부분이 이미 60일 고가 근처에 있음 (sector
rotation, 시장 강세 등 일반 패턴). 차단 trigger 가 평상 day 도 잡음.

### 5.2 overextension 지표의 outcome 예측력 없음

`close_to_high_60d / r20d / r5d` 는 "주가가 얼마나 올랐는가" 만 측정. 모멘텀
strategy 에서는 이게 entry signal 의 정확히 본질. 그래서 손절/익절 구분
능력 거의 0.

→ overextension 자체보다 **모멘텀의 지속성 / 약화 시그널** (예: RSI divergence,
거래량 감소, MACD turning) 이 진짜 outcome 예측 변수. G2 v1 의 지표는 부적합.

### 5.3 5-15 같은 special day 의 특수성

5-15 손절 차단률 100% 는 KOSPI -4% 시점에 모든 모멘텀 종목이 일제히 무너진
시장 전반 충격. 이는 **G2 (개별 종목 overextension) 가 아닌 G6 (thesis-aware,
KOSPI gate condition)** 또는 매크로 throttle 의 영역. G2 임계값 잡기 부적합.

## 6. 결정 — G2 구현 보류

design §7.4 룰 적용:
- "평상 day 차단률 > 50% 시 도입 보류" → **충족 (평상 day 평균 70%+)**
- 추가 발견: "익절 차단률 > 손절 차단률" → 도입 시 명백히 negative EV

**결론**: 현 임계값 + 지표로 G2 v1 구현 시 운영 손실 가능성 높음. **구현 보류**.

## 7. 다음 옵션 (별도 세션)

### Option A — G2 임계값 재설계 (지표 자체 변경)
- overextension 지표 (가격 기반) → **모멘텀 약화 지표** 로 전환
- 후보: RSI divergence, MACD turning, 거래량 감소, 시초 갭다운 비율
- 새 임계값 grid search 후 재 Pre-flight

### Option B — G2 적용 범위 축소
- GAP_UP_REBOUND 만 적용 (sample 11 누적 후 30 도달 시 재검토)
- 다른 strategy 는 skip
- 가치 작지만 명백한 positive

### Option C — G2 폐기 + G6 우선
- G2 의 본질이 G6 (thesis-aware) 에 흡수 가능 — kospi_change_pct_above 같은
  catalog 조건이 5-15 같은 시점에 G2 대신 작동
- G6 Phase A 부터 진행, G2 는 long-term 폐기
- 가장 정직한 선택 — G6 가 더 일반화된 메커니즘

### Option D — 다른 가드 우선
- G3 (시초 갭다운 entry) 또는 G4 (시초 추격 손절 timing)
- 5-15 사고의 다른 면을 다룸

## 8. 학습 / 메모리화 후보

- **single-day 데이터의 임계값 over-fitting 위험** — 5-15 8/10 차단을 충분 근거로
  본 design v1 의 실수. Pre-flight 가 정확히 그걸 잡음. 향후 가드 설계 원칙:
  **n=1 day 의 정량 검증은 임계값 시작점일 뿐 검증 X**. 최소 N>30 sample +
  outcome 구분 능력 측정 필수.
- **모멘텀 strategy 의 paradox** — "이미 오른 종목" 이 entry signal 인 strategy
  에서 "이미 오른 종목" 차단 가드는 entry signal 부정. SECTOR_MOMENTUM 의 익절
  차단률 85.7% 가 그 증거.
- **본질적 outcome 예측 변수 찾기 어려움** — 가격 기반 지표는 outcome 구분 못함.
  진짜 예측 변수는 timing (시초 갭다운, 시장 반전) 또는 thesis (KOSPI gate 깨짐)
  같은 **상태 변화** 신호. G3 / G6 의 방향이 맞음.

## 9. 산출물

- 본 문서: `.ai/analyses/2026-05-17-g2-thresholds-historical-validation.md`
- 영향: G2 design v3.1 의 §6.1 도입 일정 보류 표시 + §7.4 결과 반영 필요 (별도 갱신)
- task 정리: Pre-flight 완료, 구현/테스트/메모리 task 모두 deleted

## 10. 참조

- design: `.ai/designs/2026-05-17-g2-overextension-validator.md`
- handoff: `.ai/sessions/session-2026-05-15-0003.md` §G1 (sample 16 의 EARNINGS_DRIFT 87.5%)
- 메모리 후보: 새 `feedback_single_day_overfit.md` (`feedback_audit_layers` 와 동일 결).
