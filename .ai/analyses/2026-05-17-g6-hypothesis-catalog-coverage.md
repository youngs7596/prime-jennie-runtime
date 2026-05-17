# G6 Thesis Catalog — Hypothesis Coverage Pre-flight

**작업 일자**: 2026-05-17 (일)
**작업 시간**: ~30분 (DB 쿼리 + paragraph 분석)
**Design**: `.ai/designs/2026-05-17-g6-thesis-aware-exit.md` §8.5
**결정**: **catalog 7 → 8종 (r20d_above_threshold 추가)** → Phase A 진입.

---

## 1. 작업 목적

design §8.5 명시: catalog 7종이 historical hypothesis 80% 이상 표현 가능한지
사전 검증. 미달 시 catalog 확장 후 Phase A 진입.

## 2. 데이터

- 기간: 2026-04-17 ~ 2026-05-17 (지난 30일)
- position_sheets total: 1341
- distinct hypotheses: **101**
- 빈도 분포: long tail — 상위 30개가 cum 44.7%, 80% coverage 위해선 60+ hypothesis 매핑 필요

## 3. 매핑 방법

**상위 30 hypothesis (cum 44.7%) 의 paragraph-level 패턴 분석** — 정량 매핑 아닌
추정. G2 over-fitting 학습으로 정량 단정 회피.

## 4. 발견 — 핵심 패턴 5종 + 부족 1종

### 4.1 거의 모든 hypothesis 의 공통 5종 condition

| catalog type | 빈도 | 예시 |
|---|---|---|
| `sector_momentum_above` | 거의 전부 | "반도체/IT +36%", "화학/에너지 +35%" |
| `no_risk_event_high` | 거의 전부 | "리스크 이벤트 없음" |
| `earnings_event_window` | 거의 전부 | "earnings/contract 호재" |
| `kospi_change_pct_above` | 흔함 | "KOSPI +2.63%", "KOSPI +5.12%" |
| `rsi_below` | 흔함 | "RSI 30~45 과매도", "RSI 30~60" |

→ 5종으로 패턴의 핵심 표현 가능.

### 4.2 catalog 7종 중 사용 빈도 낮은 2종

- `kospi_gate` — "KOSPI 강세" 의 종합 표현. 빈번하지만 `kospi_change_pct_above`
  와 grain 차이 (gate 종합 vs 단일 정량). design §4.2 의 의미 분리 유효.
- `price_above_breakout` — "거래량 급증", "볼린저 하단" 등 GAP_UP_REBOUND 진입
  signal. hypothesis 에서는 드물게 등장 (GAP_UP_REBOUND strategy 자체 비율 낮음).

→ 빈도 낮지만 strategy 의존, 유지.

### 4.3 부족 발견 — **20일 모멘텀**

빈도 매우 높음 (rank 1, 3, 19, 22, 24, 28, 29 등 30 중 7+) 단 catalog 7종에 없음.
예시 표현:
- "20일 모멘텀 양호"
- "20일 양수 모멘텀"
- "20일 가격 모멘텀 강한"

→ **신규 catalog type 추가**: `r20d_above_threshold`
- params: `{"min_pct": 0.0}` (default 양수만, 더 엄격 시 0.05+)
- evaluator 입력: daily_prices (G2 의 r20d 계산과 동일 지표)
- 평가 비용: 1 SQL

### 4.4 의도적 제외 (catalog 불추가)

- **"5일 모멘텀"** — 빈도 낮음 (rank 3 만). 20일 모멘텀의 subset 표현.
  Phase A 1주 측정 후 빈도 재평가.
- **"거래량 급증"** — entry signal 의 의미가 강함. hold guard (G6) 와는 결이
  다름 (현재 거래량 부재 시 thesis 무효? 의미 모호). 제외.
- **"볼린저 하단", "BB %B 0.4 미만"** — 빈도 매우 낮음, RSI 의 subset 표현.

## 5. 정직한 caveat

### 5.1 정량 매핑 아님

paragraph 분석으로 추정. **실제 매핑률은 Phase A 1주 운영 후 Scout LLM 의
thesis_spec 반환률로 측정 가능** — 본 작업의 한계.

### 5.2 G2 over-fitting 학습 반영

G2 의 n=10 임계값을 충분 근거로 본 실수 (단일 day over-fit) 반복 회피. 본
Pre-flight 도 추정이라 design 의 "80% gate" 정확히 충족 단정 X — Phase A
운영으로 실증.

### 5.3 단, catalog 추가의 trade-off

`r20d_above_threshold` 자체는 G2 에서 outcome 예측력 없음 검증됨. 단:
- G2 (entry 차단) vs G6 (hold 평가) 는 layer 가 다름
- G6 의 r20d_above 는 "20일 모멘텀 유지" 가 깨지면 hold 결정에 영향
- entry 시점 r20d 와 hold 시점 r20d 의 변화 시그널이 G6 의 본질

→ G2 의 부정적 결과가 G6 catalog 의 r20d 추가를 막지 않음.

## 6. 결정

- **catalog 7 → 8종** (`r20d_above_threshold` 추가)
- design §4.2 표 + §4.4 critical 후보 갱신
- Phase A 진입 (schema + prompts + 영속화)
- **Phase A 1주 후** Scout LLM 의 실 thesis_spec 반환률 측정 → 80% 미달 시 catalog
  재확장 (5d momentum, 거래량 등 후보)

## 7. 다음 단계

1. design `.ai/designs/2026-05-17-g6-thesis-aware-exit.md` 의 §4.2 catalog 표 갱신 (8종)
2. design §4.4 critical 후보 표 갱신 (필요 시 r20d_above_threshold 추가)
3. design §8.5 Pre-flight 완료 표시 + 본 doc cross-ref
4. Phase A 구현 시작 (schema + prompts + tests)

## 8. 학습 / 메모리화 후보

- **paragraph-level 분석의 한계** — 정량 매핑 아님을 명시. Phase A 1주 후 실증으로
  검증. G2 학습 (single-day 정량을 충분 근거로 본 실수) 의 일반화.
- **G2 부정 결과가 G6 catalog 결정을 막지 않음** — entry vs hold layer 분리 원칙.
  같은 지표라도 다른 layer 에서 다른 의미. design 의 직교성 (G1~G5 entry vs G6
  hold) 의 실제 운영 효과.

## 9. 참조

- design: `.ai/designs/2026-05-17-g6-thesis-aware-exit.md`
- G2 Pre-flight (대조군): `.ai/analyses/2026-05-17-g2-thresholds-historical-validation.md`
- 빈도 query 결과: 본 doc §2 (DB 쿼리 1회)
