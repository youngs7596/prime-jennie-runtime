# Entry Conditions 정책 채택 현황

> POSITION_SHEET_SPEC §4.2 의 4종 EntryCondition (price_below / price_above /
> volume_over_ma20 / spread_under_bps) 가 운영 정책에 어떻게 부착되어 있는지의
> 점진 갱신 기록.

## 채택 상태 매트릭스

| Condition | Evaluator 구현 | 정책 부착 위치 | 상태 |
|-----------|---------------|---------------|------|
| `spread_under_bps` | `fast_loop/pending_entry._evaluate_one` | strategy_policy.yaml 의 모든 strategy 의 `default_entry_conditions` | active (v3.0.2+) |
| `price_above` | 동일 | engine.py — GAP_UP_REBOUND 의 자동 부착 (scout price_hint 기반) | partial (v3.0.4, 2026-05-11) |
| `price_below` | 동일 | 정책 미부착, scout 가 conditions_hint 로만 추가 가능 | scout-only |
| `volume_over_ma20` | unsupported (ma20 lookup 미구현) | 정책 미부착 | blocked |

## price_above 자동 부착 경로 (v3.0.4)

### 의도
시초 갭상승 후 직전 봉 high 돌파 확인 후 진입. v2 buy-scanner 의 "직전 봉 high
+ 0.1%" 휴리스틱과 등가.

### 부착 조건 (`strategy/engine.py`)
1. `strategy_tag == "GAP_UP_REBOUND"`
2. `candidate.entry_hint.price_hint` 가 None 이 아니고 > 0
3. scout 가 conditions_hint 에 `price_above` 를 이미 박아두지 않음

### 부착 값
```
price_above.value = price_hint × PRICE_ABOVE_BREAKOUT_MULT
                  = price_hint × 1.001  (+10bps 정적 fallback)
```

`PRICE_ABOVE_BREAKOUT_MULT` 상수는 `engine.py` 상단에 정의. 운영 데이터 누적
후 정량 조정 검토 (현재 1.001 은 v2 휴리스틱 + 미세 호가 매칭 동등).

### 향후 정밀화 (Scout F agent 작업 예정)
- Scout 가 직전 봉 high 또는 시가 + α 를 명시적으로 `price_above` conditions_hint
  로 박아주면 engine 의 자동 부착 분기가 자동 skip 됨 (`any(c.type=="price_above")`).
- Scout 가 channel 분석으로 정확한 breakout level 을 계산하면 정적 1.001 보다
  정밀한 임계 사용 가능.
- 그 시점에 engine 의 `PRICE_ABOVE_BREAKOUT_MULT` 자동 부착 로직 제거 검토.

## price_below / volume_over_ma20

### price_below
Scout 가 mean-reversion 가설 (MEAN_REVERT_RSI 등) 에서 "특정 가격대 이하로
조정 시 매수" 패턴을 conditions_hint 로 박을 때만 활성. 정책 yaml 자동 부착
계획 없음 — scout 의도가 종목별 분석에 의존.

### volume_over_ma20
ma20 일거래량 lookup path 미구현이라 evaluator 가 unsupported 반환 → 시트
진입이 차단. 정책 부착 시 시트 전수가 entry block 되므로 의도적으로 미부착.
다음 단계: `fast_loop` 에 ma20 일거래량 fetcher 구현 → 정책 부착.

## 관련 파일

- `prime_jennie_runtime/position_sheet/schema.py` — 4종 EntryCondition 스키마
- `prime_jennie_runtime/slow_loop/strategy/engine.py` — sheet 조립 시 부착 로직
- `prime_jennie_runtime/slow_loop/strategy/strategy_policy.yaml` — 정책별 default
- `prime_jennie_runtime/fast_loop/pending_entry.py` — EntryConditionEvaluator
- `docs/POSITION_SHEET_SPEC.md` §4.2 — 스키마 명세
