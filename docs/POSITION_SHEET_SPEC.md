# POSITION_SHEET_SPEC

> **문서 목적**: 포지션 시트 JSON의 전수 명세. 발행자(Strategy Engine), 소비자(Executor), 관찰자(Control UI, meta Eval)가 이 문서 하나로 합의한다.
>
> **선행 문서**: `prime_jennie_v3_phase0_design.md` §5.1
>
> **schema_version**: 1.1
> **작성자**: 민지 × 영석
> **작성일**: 2026-04-16
> **버전**: 0.2

---

## CHANGELOG

### v0.3 (2026-06-12)
사람-승인 매매 설계 반영 (`.ai/designs/2026-06-12-human-approved-trading-nl-interface.md` §3-1).

- **§5.2.10 `recovery_exit` rule 신설**: 손익률이 임계(0 이하) 이상으로 회복하면 전량 청산.
  운영자가 텔레그램 자연어로 등록하는 조건부 매도용 — Scout 전략은 이 rule 을 발행하지 않는다.

### v0.2 (2026-04-16)
Claude Code v2 컨텍스트 리뷰 반영. v2의 12-rule 체계 커버리지 완성.

- **§5.2.8 `profit_floor` rule 신설**: 고점 +15% 도달 후 +10% 바닥 사수 (v2 검증). `trailing_tp`와의 차이 명시.
- **§5.2.9 `death_cross` rule 신설**: 5MA/20MA 하향 교차 + 손실 구간 조건. 일봉 기준, `min_loss_pct` 필수.
- **§5.3 권장 순서 갱신**: 8개 rule 순서 재배치. profit_floor는 trailing_tp보다 위, death_cross는 fixed_sl 바로 위.
- **§11 테스트 케이스 6건 추가** (T15~T20): profit_floor / death_cross 시나리오.

### v0.1 (2026-04-16)
초안.

---

## 1. 버전 및 호환성 정책

### 1.1 버전 체계

`schema_version`은 `MAJOR.MINOR` 2자리.

- **MAJOR 증가**: 기존 필드 의미 변경, 필드 삭제, 필드 타입 변경. 하위 호환 깨짐.
- **MINOR 증가**: 필드 추가 (optional), 새 enum 값 추가. 하위 호환 유지.

### 1.2 소비자 동작

Executor는 `schema_version`을 읽고:
- **MAJOR 불일치**: 시트 거부. `pj.executor.schema_incompatible` observer 이벤트 발행. 청산 영향 없음(기존 포지션 유지).
- **MINOR 불일치 (소비자가 더 낮음)**: 경고 로그. 아는 필드만 파싱, 모르는 필드 ignore. 처리는 계속.
- **MINOR 불일치 (소비자가 더 높음)**: 정상 처리.

### 1.3 현재 지원 버전

| schema_version | 상태 | 소비자 |
|---|---|---|
| 1.0 | deprecated (v0.1 초안) | 없음 |
| **1.1** | **current** | Executor v3.0.0+ |

---

## 2. 최상위 필드

```typescript
interface PositionSheet {
  sheet_id: string            // "ps_{YYYYMMDD}_{ticker}_{4자 hex}"
  schema_version: "1.1"
  generated_at: ISO8601       // KST offset 필수 "+09:00"
  valid_until: ISO8601        // KST offset 필수
  
  ticker: string              // "005930" 같은 6자리 KRX 코드
  side: "long"                // 현재 long만 지원. short는 v2.0에서
  strategy_tag: string        // 사전 정의된 enum (§2.3)
  
  size: SizeSection           // §3
  entry: EntrySection         // §4
  exit: ExitSection           // §5
  provenance: ProvenanceSection  // §6
}
```

### 2.1 sheet_id 규칙

정규식: `^ps_\d{8}_\d{6}_[0-9a-f]{4}$`

- `YYYYMMDD`: 발행일 KST 기준
- `6자리 숫자`: KRX 티커
- `4자 hex`: 충돌 방지용 랜덤

**불변식**: 같은 날 같은 ticker에 **동일 sheet_id 중복 발행 금지**. Strategy Engine은 발행 전 Postgres `position_sheets` 테이블에 `sheet_id` unique index 위반 체크.

### 2.2 시각 필드

**모든 시각 필드는 KST(+09:00) ISO8601**. UTC 사용 금지. 이유는 단순함: 영석님과 사람이 로그를 읽을 때 오프셋 변환 안 해도 되게.

```
OK:  "2026-04-16T09:15:00+09:00"
OK:  "2026-04-16T09:15:00.123+09:00"
NG:  "2026-04-16T00:15:00Z"          ← UTC 금지
NG:  "2026-04-16T09:15:00"            ← 오프셋 누락 금지
```

**불변식**:
- `generated_at <= valid_until - 60s` (최소 60초 유효)
- `valid_until <= 당일 15:30 KST` (장 마감 넘어서는 시트는 time_stop 처리로)
- `entry.valid_until <= valid_until`

### 2.3 strategy_tag enum

v0.2 기준 지원 값. 추가는 MINOR 업데이트.

| strategy_tag | 설명 | 상태 |
|---|---|---|
| `GAP_UP_REBOUND` | 갭 상승 후 되돌림 진입 | 주력 (v2 검증) |
| `SECTOR_MOMENTUM` | 섹터 모멘텀 팔로우 | 주력 |
| `EARNINGS_DRIFT` | 실적 서프라이즈 드리프트 | 실험 |
| `MEAN_REVERT_RSI` | RSI 과매도 평균회귀 | 보조 |

**RSI_REBOUND는 폐기됨** (v2 실험 결과). Scout가 이 태그를 생성하면 Strategy Engine이 거부.

---

## 3. `size` 섹션

```typescript
interface SizeSection {
  base_pct: number            // 0 < x <= 0.10
  macro_multiplier: number    // 0 <= x <= 1.0
  risk_multiplier: number     // 0 <= x <= 1.0
  final_pct: number           // = base_pct * macro_multiplier * risk_multiplier
  max_notional_krw: number    // 절대 상한 (원)
}
```

### 3.1 각 필드 의미

- **base_pct**: 전략 기본 크기. `strategy_policy.yaml`에서 strategy_tag별로 정의. 영석님이 수동 조정.
- **macro_multiplier**: Macro Gate 출력 그대로 복사. 어떤 변형도 금지.
- **risk_multiplier**: Intraday Risk Throttle의 **시트 발행 시점** 스냅샷. 발행 후 Throttle이 바뀌어도 이 값은 고정.
- **final_pct**: 세 값의 곱. 부동소수 오차 허용 (`abs(final - base*macro*risk) < 1e-9`).
- **max_notional_krw**: `final_pct * 계좌자산 > max_notional_krw`면 max로 clamp. 단일 종목 집중 방지.

### 3.2 불변식

```python
assert 0 < base_pct <= 0.10        # 단일 종목 10% 초과 금지
assert 0 <= macro_multiplier <= 1.0
assert 0 <= risk_multiplier <= 1.0
assert 0 <= final_pct <= 0.10
assert abs(final_pct - base_pct * macro_multiplier * risk_multiplier) < 1e-9
assert max_notional_krw > 0
```

### 3.3 final_pct 하한

`MIN_POSITION_PCT = 0.005` (0.5%) 미만은 Strategy Engine이 시트 **미발행**. 너무 작은 포지션은 수수료 대비 비효율.

---

## 4. `entry` 섹션

```typescript
interface EntrySection {
  trigger: "limit" | "market"
  price: number | null         // limit이면 필수, market이면 null
  valid_until: ISO8601         // <= sheet.valid_until
  conditions: EntryCondition[] // 추가 필터 (AND 조건)
}

type EntryCondition =
  | { type: "price_below", value: number }
  | { type: "price_above", value: number }
  | { type: "volume_over_ma20", min_ratio: number }
  | { type: "spread_under_bps", max_bps: number }
```

### 4.1 trigger type별 동작

**`limit`**:
- `price`에 지정가 주문 발행
- `valid_until`까지 미체결이면 주문 취소, 시트 소멸
- 호가창 확인 후 `conditions` 전부 만족 시에만 발행

**`market`**:
- `price`는 null
- 발행 즉시 시장가 주문
- `conditions`는 발행 직전 snapshot 체크 용도

### 4.2 EntryCondition 상세

| type | 파라미터 | 동작 |
|---|---|---|
| `price_below` | `value` | 현재가 < value일 때만 진입 |
| `price_above` | `value` | 현재가 > value일 때만 진입 (breakout) |
| `volume_over_ma20` | `min_ratio` | 금일 거래량 > 20일 평균 × ratio |
| `spread_under_bps` | `max_bps` | 호가 스프레드가 max_bps 이하 |

**conditions 전부 AND**. 어느 하나라도 실패 시 진입 보류. `valid_until` 내 재평가.

### 4.3 엣지 케이스 — 장 시작 직전 시트

`generated_at`이 **09:00 KST 이전**이면:
- Executor는 시트를 받아도 **장 개장까지 대기**
- 09:00 동시호가 직전 `conditions` 재평가
- 동시호가 체결가가 `price_below` 등을 위반하면 진입 포기

---

## 5. `exit` 섹션 — 핵심

```typescript
interface ExitSection {
  rules: ExitRule[]            // 최소 1개, fixed_sl 필수 포함
  priority: "first_match"      // 현재 first_match만 지원
}

type ExitRule =
  | TrailingTpRule
  | FixedTpRule
  | FixedSlRule
  | BreakevenRule
  | ScaleOutRule
  | TimeStopRule
  | OverextensionExitRule
  | ProfitFloorRule
  | DeathCrossRule
  | RecoveryExitRule
```

### 5.1 평가 주기

Executor는 **틱 수신마다** exit.rules[]를 **배열 순서대로** 평가한다. 처음 매칭되는 rule이 액션을 수행한다.

**예외**: `breakeven`은 SL 수정만 하고 청산하지 않음. `scale_out`은 부분 청산 후 나머지를 계속 관리. 이 두 rule은 "첫 매칭"이지만 포지션 유지.

### 5.2 rule type 전수 명세

#### 5.2.1 `fixed_sl` — 필수

```typescript
{ type: "fixed_sl", pct: number }
```

- `pct`: 진입가 대비 손실 비율 (양수, 0 < pct <= 0.10)
- 트리거: `현재가 <= 진입가 * (1 - pct)`
- 액션: 시장가 전량 청산
- `exit_reason`: `"sl"`

**강제 규칙**: 모든 포지션 시트는 `fixed_sl` 1개 필수. 없으면 Strategy Engine이 발행 단계에서 거부.

**breakeven과 상호작용**: breakeven 발동 후에는 `fixed_sl` 대신 breakeven이 설정한 `floor_pct`가 유효 SL. 둘 다 rules[]에 있으면 breakeven이 "동적으로" `fixed_sl`을 덮어씀 (§5.2.4).

#### 5.2.2 `fixed_tp`

```typescript
{ type: "fixed_tp", pct: number }
```

- `pct`: 진입가 대비 익절 비율 (양수)
- 트리거: `현재가 >= 진입가 * (1 + pct)`
- 액션: 시장가 전량 청산
- `exit_reason`: `"tp"`

#### 5.2.3 `trailing_tp`

```typescript
{ type: "trailing_tp", activate_pct: number, drop_pct: number }
```

- `activate_pct`: 진입가 대비 몇 % 상승 시 활성화
- `drop_pct`: 활성화 후 고점 대비 몇 % 하락 시 청산
- Executor는 `peak_price`를 포지션 내부 상태로 유지
- 트리거:
  - 비활성 상태: `현재가 >= 진입가 * (1 + activate_pct)` → 활성화, `peak_price = 현재가`
  - 활성 상태: `현재가 >= peak_price` → `peak_price` 갱신
  - 활성 상태: `현재가 <= peak_price * (1 - drop_pct)` → 청산
- `exit_reason`: `"tp"`

#### 5.2.4 `breakeven` — 청산 안 함

```typescript
{ type: "breakeven", activate_pct: number, floor_pct: number }
```

- `activate_pct`: 이 비율 상승 시 활성화
- `floor_pct`: 활성화 후 진입가 대비 유지할 최소 이익 (양수)
- 활성화 조건: `현재가 >= 진입가 * (1 + activate_pct)` **1회 달성** 시 활성화 (이후 가격 하락해도 활성 상태 유지)
- 활성화 후 동작:
  - 유효 SL 가격 = `진입가 * (1 + floor_pct)`
  - 기존 `fixed_sl` rule의 트리거 가격이 이보다 낮으면 **breakeven 가격으로 상향**
  - 현재가 <= 유효 SL → 청산
  - `exit_reason`: `"breakeven"`

**v2 검증 파라미터**: `activate_pct=0.03, floor_pct=0.003` (3% 도달 후 0.3% 바닥).

**이유**: 진입 후 3% 이익 구간에 도달하면 최소한 수수료 + 소액 이익은 확보하고 나온다. v2 실전에서 "익절 못 하고 손실로 끝나는" 패턴을 크게 줄임.

#### 5.2.5 `scale_out` — 부분 청산

```typescript
{ type: "scale_out", levels: [number, number][] }
```

- `levels`: `[[trigger_pct, portion_pct], ...]` 배열
  - `trigger_pct`: 진입가 대비 상승률
  - `portion_pct`: 해당 레벨에서 청산할 **초기 포지션 대비** 비율
- 예: `[[0.03, 0.25], [0.05, 0.25]]` → 3%에서 25% 청산, 5%에서 또 25% 청산, 잔여 50%는 다른 rule로
- 각 레벨은 **1회만** 실행
- Executor는 `scale_out_executed: set[int]`를 포지션 상태로 유지
- `exit_reason`: `"scale_out"` (부분 청산 기록)

**불변식**: `sum(portion_pct) <= 1.0`. 전량 청산은 다른 rule(trailing_tp, fixed_sl 등)이 담당.

**상호작용**: scale_out이 일부 청산한 후에도 남은 포지션에 대해 `fixed_sl`, `trailing_tp`, `breakeven`이 계속 유효. 손절 가격은 **원래 진입가 기준** 그대로 유지 (부분 매도가 평균 단가 왜곡하지 않음).

#### 5.2.6 `time_stop`

```typescript
{ type: "time_stop", mode: "eod" | "hold_days", value?: number }
```

**mode별**:
- `"eod"`: 당일 15:20 KST에 시장가 청산. `value` 무시.
- `"hold_days"`: 진입 후 `value`영업일 경과 시 15:20 청산. `value >= 1`.

- `exit_reason`: `"time"`

**15:20 선택 이유**: 종가 동시호가(15:20~15:30) 직전에 청산하여 슬리피지 최소화. v2에서 15:28까지 버티다 미체결되는 사고 있었음.

#### 5.2.7 `overextension_exit`

```typescript
{ type: "overextension_exit", rsi_threshold: number }
```

- `rsi_threshold`: 1분봉 RSI(14)가 이 값 초과 시 청산 (일반적으로 85 이상)
- v2의 Overextension Filter와 연동
- 트리거: 매 1분봉 마감 시 RSI 계산 → threshold 초과면 다음 틱에서 시장가 청산
- `exit_reason`: `"overextension"` (별도 사유 추가)

#### 5.2.8 `profit_floor`

```typescript
{ type: "profit_floor", activate_pct: number, floor_pct: number }
```

- `activate_pct`: 고점 수익률이 이 값 이상에 **1회 도달** 시 활성화 (v2 검증: 0.15)
- `floor_pct`: 활성화 후 **수익률이 이 값 미만**으로 떨어지면 전량 청산 (v2 검증: 0.10)
- Executor는 `peak_return_pct`를 포지션 내부 상태로 유지
- 트리거:
  - 비활성: `(현재가 / 진입가 - 1) >= activate_pct` → 활성화 + `peak_return_pct` 갱신
  - 활성: `(현재가 / 진입가 - 1) > peak_return_pct` → `peak_return_pct` 갱신
  - 활성: `(현재가 / 진입가 - 1) < floor_pct` → 시장가 전량 청산
- `exit_reason`: `"profit_floor"`

**`trailing_tp`와의 차이**:

| 축 | `trailing_tp` | `profit_floor` |
|---|---|---|
| 기준 | 고점 대비 **하락폭** | 진입가 대비 **절대 수익률** 바닥 |
| 예시 | 고점 +20%에서 3% 하락 시 +17%에서 청산 | 고점 +20% 찍어도 +10% 바닥 깨지면 청산 |
| 용도 | 수익 구간 폭넓게 유지 | 큰 수익 확보 후 바닥 사수 |

둘은 **같은 rules[]에 동시 존재 가능**. 배열 순서상 먼저 매칭되는 쪽이 실행.

**v2 검증 파라미터**: `activate_pct=0.15, floor_pct=0.10`. 고점 +15% 도달 후 +10% 바닥 유지.

**이유**: v2 실전에서 대박 종목이 고점에서 절반 이상 반납하는 패턴이 반복됐음. trailing_tp만으로는 고점이 계속 갱신되면 drop_pct가 커진 만큼 깊게 반납. profit_floor는 절대 수익률 바닥을 지켜 큰 수익의 대부분을 확보.

#### 5.2.9 `death_cross`

```typescript
{ type: "death_cross", ma_short: number, ma_long: number, min_loss_pct: number }
```

- `ma_short`, `ma_long`: 이동평균 기간 (v2 검증: 5, 20, **일봉 기준**)
- `min_loss_pct`: 이 값 이상 손실 구간에서만 발동 (v2 검증: 0.01 — -1% 이상 손실일 때만)
- 트리거:
  - `MA(ma_short)`이 `MA(ma_long)`을 **하향 교차** (전일 MA5 > MA20, 금일 MA5 <= MA20)
  - **AND** `(1 - 현재가 / 진입가) >= min_loss_pct`
- 액션: 시장가 전량 청산
- `exit_reason`: `"death_cross"`

**주의사항**:
- **일봉 기준** 교차 판정. 장중 실시간이 아닌 전일 종가 확정 후 금일 시가~장중 판정.
- Executor는 매일 09:00에 전일 일봉 MA 값 로드 후 해당 포지션 대상 1회 체크.
- 이동평균은 **종가 기준 단순이동평균**. 지수이동평균은 별도 type으로 추후 확장.

**`min_loss_pct` 조건이 필요한 이유**: 수익 구간에서 death cross는 빈번하게 발생하지만 추세 반전 시그널로서 신뢰도 낮음. 손실 구간 + 추세 꺾임이 결합되어야 청산 가치 있음. 수익 구간은 trailing_tp/profit_floor/breakeven이 담당.

**v2 검증 파라미터**: `ma_short=5, ma_long=20, min_loss_pct=0.01`.

#### 5.2.10 `recovery_exit`

```typescript
{ type: "recovery_exit", pct: number }   // -0.10 <= pct <= 0.0
```

- `pct`: 청산 임계 손익률, **0 이하 분수**. 예: `-0.01` = "손실이 -1% 위로 줄어들면 매도", `0.0` = "양전하면 매도"
- 트리거: `(현재가 / 진입가 - 1) >= pct`
- 액션: 시장가 전량 청산
- `exit_reason`: `"recovery_exit"`

**용도**: 운영자가 텔레그램 자연어로 등록하는 조건부 매도 (2026-06-12 사람-승인 매매 설계).
물린 보유를 "본전 근처로 회복하면 정리" 하는 의도의 표현. **Scout 전략은 이 rule 을
발행하지 않으며**, 양수 목표 익절은 기존 `fixed_tp` 의 역할이라 pct 양수는 스키마가 거부한다.

**주의사항**:
- 등록 시점에 이미 손익률이 임계 이상이면 **다음 tick 에서 즉시 발동**한다. 등록 확인
  단계에서 발동가를 표기해 사용자가 인지한 상태로 등록하게 한다.
- breakeven 처럼 상태를 갖지 않는 무상태 rule — 매 tick 단순 비교.

### 5.3 rules[] 배열 순서 규칙

**권장 순서** (first_match 평가 순):

```json
"rules": [
  { "type": "overextension_exit", ... },   // 과열 최우선
  { "type": "profit_floor", ... },         // 큰 수익 바닥 사수
  { "type": "trailing_tp", ... },          // 수익 보호
  { "type": "scale_out", ... },            // 부분 익절
  { "type": "breakeven", ... },            // 손익분기 보호
  { "type": "death_cross", ... },          // 추세 반전 + 손실
  { "type": "fixed_sl", ... },             // 최후 방어
  { "type": "time_stop", ... }             // 시간 종료
]
```

이유:
- `fixed_sl`은 **항상 마지막 두 번째**. 다른 모든 조건 실패 후 최후 방어.
- `time_stop`은 **마지막**. 다른 rule이 청산하지 않은 경우에만 발동.
- `overextension_exit`은 **최상단**. 과열 상태는 다른 어떤 rule보다 우선.
- `profit_floor`는 `trailing_tp`보다 **상위**. 큰 수익 확보 우선.
- `death_cross`는 `fixed_sl` 바로 위. 손실 구간 추세 반전 시 fixed_sl 도달 전 조기 청산.

Strategy Engine은 이 권장 순서를 기본값으로 생성. 전략별로 커스텀 가능하나 `fixed_sl`, `time_stop`은 항상 존재.

---

## 6. `provenance` 섹션

```typescript
interface ProvenanceSection {
  scout_run_id: string           // scout_runs 테이블 FK
  scout_code_hash: string        // sha256:...
  scout_hypothesis: string       // 자연어 가설 복사본
  macro_state_snapshot: {
    gate: "open" | "closed" | "manual_override"
    size_multiplier: number
    gate_run_id: string          // macro_runs 테이블 FK
  }
  macro_run_id: string | null              // top-level FK (gate_run_id 와 동일, 인덱스 단순화)
  news_score_at_generation: number | null  // -1.0 ~ +1.0
  strategy_policy_version: string          // "v3.0.1" semver
  generated_by: string                      // "prime-jennie-runtime@v3.0.1"
  conviction: number | null                 // Scout 발행 conviction (0.0~1.0), Phase 0 #1 correlation 용
  thesis_spec: ThesisSpec | null            // G6 thesis_aware_hold Phase A (2026-05-17)
}

interface ThesisSpec {                    // 2026-05-17 신규
  natural_language: string                  // scout_hypothesis 와 동일 호환
  conditions: ThesisCondition[]             // 검증 가능한 조건 list (catalog 8종 — Phase A v0.8)
  critical_conditions: number[]             // conditions index — 깨지면 즉시 invalidated (Phase 2 enforce 시 trigger)
}

interface ThesisCondition {
  type: "kospi_gate" | "kospi_change_pct_above" | "sector_momentum_above"
      | "no_risk_event_high" | "earnings_event_window" | "rsi_below"
      | "price_above_breakout" | "r20d_above_threshold"
  params: Record<string, any>               // type 별 schema (evaluator 가 검증)
}
```

### 6.1 왜 이 필드들인가

meta Eval이 "이 매매가 왜 들어갔는가"를 3개월 뒤에도 재구성할 수 있어야 한다. Provenance가 부실하면 self-evolving이 무의미해진다.

- `scout_run_id` + `scout_code_hash`: 정확히 어떤 Scout 코드가 만들었는지
- `scout_hypothesis`: 가설 자체 (Scout가 무엇을 노렸나)
- `macro_state_snapshot`: 그 시점 매크로 판단
- `news_score_at_generation`: 뉴스 감성 (Scout 입력 중 가장 변동성 큰 것)
- `strategy_policy_version`: 룰셋 버전
- `generated_by`: 발행 코드 버전
- `conviction`: Scout 의 자신감 (Phase 0 #1 conviction-outcome correlation 분석 용)
- `thesis_spec`: G6 thesis_aware_hold 가드의 검증 가능한 hypothesis 조건. **None 허용** (Phase A 도입 전 sheet 호환). revaluator (Phase 1 advisory, 5-22~) 가 보유 중 정기 평가, invalidated 시 `forced_liquidation:thesis` Redis SET 적재 (Phase 2, 5-29~). 단순화 결정: [`.ai/designs/2026-05-17-g-series-simplification.md`](../.ai/designs/2026-05-17-g-series-simplification.md). catalog v1 은 5종 (kospi_gate / sector_momentum_above / no_risk_event_high / earnings_event_window / rsi_below) — 위 type Literal 의 나머지 3종 (kospi_change_pct_above / price_above_breakout / r20d_above_threshold) 은 schema 만 정의, Phase A 측정 후 catalog 편입 결정.

### 6.2 Scout 없이 발행된 시트

영석님이 수동으로 발행한 시트의 경우:

```json
"provenance": {
  "scout_run_id": "manual",
  "scout_code_hash": "sha256:0000...",
  "scout_hypothesis": "영석 수동 발행: <사유>",
  "macro_state_snapshot": { "gate": "manual_override", ... },
  ...
}
```

meta Eval은 `scout_run_id == "manual"`인 시트를 자동 학습 데이터에서 제외.

---

## 7. 검증 규칙 (pydantic validator 필수 구현)

Strategy Engine은 Redis 발행 **직전** 모든 검증 통과 확인. 실패 시 `pj.strategy.sheet_rejected` 이벤트 + 발행 취소.

```python
class PositionSheet(BaseModel):
    sheet_id: str
    schema_version: Literal["1.1"]
    # ... (상세 필드)
    
    @model_validator(mode="after")
    def validate_all(self):
        # 1. sheet_id 포맷
        assert re.match(r"^ps_\d{8}_\d{6}_[0-9a-f]{4}$", self.sheet_id)
        
        # 2. 시각 일관성
        assert self.generated_at < self.valid_until
        assert self.valid_until - self.generated_at >= timedelta(seconds=60)
        assert self.valid_until.time() <= time(15, 30)
        assert self.entry.valid_until <= self.valid_until
        assert self.generated_at.tzinfo == KST
        
        # 3. size 일관성
        expected = self.size.base_pct * self.size.macro_multiplier * self.size.risk_multiplier
        assert abs(self.size.final_pct - expected) < 1e-9
        assert 0 < self.size.base_pct <= 0.10
        assert self.size.final_pct >= 0.005   # MIN_POSITION_PCT
        
        # 4. entry 일관성
        if self.entry.trigger == "limit":
            assert self.entry.price is not None and self.entry.price > 0
        else:
            assert self.entry.price is None
        
        # 5. exit 일관성
        assert len(self.exit.rules) >= 1
        assert any(r.type == "fixed_sl" for r in self.exit.rules)
        assert any(r.type == "time_stop" for r in self.exit.rules)
        
        # scale_out portion 합 체크
        for rule in self.exit.rules:
            if rule.type == "scale_out":
                assert sum(p for _, p in rule.levels) <= 1.0
        
        # 6. strategy_tag
        assert self.strategy_tag in ALLOWED_STRATEGY_TAGS
        assert self.strategy_tag != "RSI_REBOUND"  # 폐기됨
        
        # 7. provenance
        if self.provenance.scout_run_id != "manual":
            # 실제 scout_runs 테이블에 존재하는지 확인 (DB 조회)
            assert scout_run_exists(self.provenance.scout_run_id)
        
        return self
```

---

## 8. Edge Case 카탈로그

실전에서 겪을 수 있는 경계 상황과 명시적 동작 정의. Executor 구현자가 전부 테스트로 커버해야 함.

### 8.1 scale_out 중간에 fixed_sl 발동

상황: 3% 도달 후 25% 부분 청산, 이후 급락하여 진입가 대비 -5%.

동작:
1. scale_out 1차 완료 후 잔여 75% 포지션 유지
2. 급락 시 `fixed_sl` 트리거 (잔여 75% 기준)
3. 잔여 75% 시장가 청산
4. `outcomes` 테이블에 **복수 exit 기록**:
   - execution 1: scale_out (25%)
   - execution 2: sl (75%)
5. `outcomes.exit_reason`은 **마지막 청산 기준** `"sl"` 기록
6. PnL 계산은 실행별 합산

### 8.2 breakeven 활성화 후 급락

상황: 진입 후 +3.5% 도달 (breakeven activate), 이후 -1% 급락.

동작:
1. breakeven 활성화, 유효 SL = `진입가 * 1.003` (floor_pct=0.003)
2. 현재가가 유효 SL 아래로 하락 → 즉시 청산
3. `exit_reason: "breakeven"`, PnL ≈ +0.3% (슬리피지 제외)

### 8.3 trailing_tp와 breakeven 모두 활성화

상황: 진입 후 +5% 도달. trailing_tp(activate=0.05) 및 breakeven(activate=0.03) 모두 활성.

동작:
1. 배열 순서 따름 (권장 순서: trailing_tp가 breakeven보다 위)
2. trailing_tp의 `peak_price` 추적 시작
3. breakeven은 유효 SL을 `진입가 * 1.003`으로 올려둠
4. 청산 조건 중 **먼저 매칭되는 것이 실행**:
   - trailing_tp drop → 청산 (`exit_reason: "tp"`)
   - 현재가가 breakeven SL 이하 → 청산 (`exit_reason: "breakeven"`)

### 8.4 장 시작 09:00 동시호가 진입

상황: 전일 23:00에 발행된 시트, `entry.trigger: "limit"`, `price: 71200`.

동작:
1. Executor는 시트를 Redis에서 받아 **대기 상태** 유지
2. 09:00 동시호가 체결가 확인
3. 체결가 <= 71200이면 진입 시도, 조건 충족 시 매수 주문
4. 체결가 > 71200이면 `entry.valid_until`까지 limit 주문 대기

### 8.5 장 마감 직전 time_stop과 fixed_sl 경쟁

상황: 15:19에 가격이 -4.9%, fixed_sl pct=0.05. 15:20 time_stop 대기.

동작:
1. 매 틱 first_match 평가
2. 15:19:58 시점 -4.8% → 매칭 없음
3. 15:19:59 시점 -5.1% → `fixed_sl` 매칭 → 즉시 시장가 청산
4. time_stop은 발동 안 함

만약 순서가 반대 (15:20 먼저 도달):
1. 15:20:00 정각 → `time_stop` 매칭 (권장 순서상 fixed_sl보다 뒤이지만 시각 조건 먼저 달성)
2. 시장가 청산, `exit_reason: "time"`

**결론**: 매 틱의 첫 매칭 기준. 시간 조건이 가격 조건보다 빨리 오면 time_stop 우선.

### 8.6 동일 ticker에 시트 중복 발행 시도

상황: Scout가 같은 날 같은 종목을 두 번 후보로 올림.

동작:
1. Strategy Engine이 발행 직전 Postgres 조회: "오늘 발행된 같은 ticker 시트 있는가?"
2. 활성 시트(valid_until 안 지남) 있으면 **두 번째 시트 발행 거부**
3. 만료된 시트만 있으면 신규 발행 허용
4. `pj.strategy.sheet_duplicate_rejected` observer 이벤트

### 8.7 Scout 환각 — universe 밖 ticker

상황: Scout 생성 코드가 context["universe"]에 없는 ticker를 반환.

동작:
1. Screening Executor가 ScreeningCandidate 리스트 반환 시 검증
2. `ticker not in universe`면 해당 candidate 필터링 (시트 발행 안 함)
3. Scout run 메타데이터에 `invalid_candidates_count` 기록
4. 한 Scout run에서 invalid 비율 30% 초과 시 `pj.scout.hallucination_suspected` 경고

### 8.8 valid_until 만료 시점에 주문 미체결

상황: entry limit 주문이 체결 안 된 채 `entry.valid_until` 도달.

동작:
1. Executor가 KIS Gateway에 주문 취소 요청
2. 취소 확인 받을 때까지 대기 (최대 10초)
3. 취소 확정 후 시트 상태를 `expired_unfilled`로 마킹
4. `outcomes` 테이블에 **빈 outcome 레코드** 생성 (pnl=0, exit_reason="unfilled")
5. meta Eval에서 unfilled 시트는 "Scout가 너무 보수적인 limit 가격을 불렀다"의 시그널

---

## 9. Redis Stream 발행 규칙

### 9.1 Stream 이름

```
position_sheets            # 메인 스트림
position_sheets.dlq        # dead letter (검증 실패)
```

### 9.2 메시지 포맷

```python
await redis.xadd(
    "position_sheets",
    {
        "sheet_id": sheet.sheet_id,
        "payload": sheet.model_dump_json(),
        "published_at": datetime.now(KST).isoformat(),
    },
    maxlen=10000,           # 스트림 최대 길이
    approximate=True,
)
```

### 9.3 Consumer Group

- Group: `executor`
- Consumer: `executor-{hostname}` (단일 인스턴스 운영 전제)

### 9.4 idempotency

Executor는 같은 `sheet_id`를 두 번 받아도 **한 번만** 처리. 내부 상태에 `processed_sheet_ids: set[str]` 유지, Postgres `position_sheets.sheet_id` unique로 DB 레벨에서도 차단.

### 9.5 DLQ 처리

검증 실패한 시트는 `position_sheets.dlq`로. 영석이 Control UI `/settings`에서 DLQ 내용 확인 가능. 자동 재처리 없음.

---

## 10. 스키마 마이그레이션 전략

### 10.1 schema_version 1.1 → 1.2 (가상 예시)

필드 추가 (optional) = MINOR. 하위 호환.

```diff
interface PositionSheet {
  // ... 기존 필드
+ hedge_pair?: string    // 선택적 헤지 페어 ticker
}
```

- Strategy Engine: 1.2 발행 시작
- Executor v3.0.0 (1.1 지원): `hedge_pair` 모름, ignore하고 정상 처리
- 문제 없음

### 10.2 1.1 → 2.0 (파괴적 변경)

- Strategy Engine: 2.0 발행 전에 **전체 Executor 업그레이드 완료** 확인
- 전환 기간: Strategy Engine에 feature flag. 환경변수로 1.1/2.0 스위치.
- 전환 완료 후 1.1 코드 경로 제거.

### 10.3 스키마 변경 책임

- `POSITION_SHEET_SPEC.md` 변경은 Phase 0 설계자(영석+민지) 결정.
- 2.0 변경은 Phase 2.5 이후 금지 (운영 안정성).
- meta가 schema 파일을 수정하는 PR은 **자동 거부**. Claude Code도 손대지 않음.

---

## 11. 테스트 케이스 (Track B 필수 구현)

| # | 케이스 | 예상 결과 |
|---|---|---|
| T01 | 정상 시트 발행 | validator 통과, Redis 발행 |
| T02 | `fixed_sl` 누락 | validator 실패, DLQ |
| T03 | `time_stop` 누락 | validator 실패 |
| T04 | `final_pct != base*macro*risk` | validator 실패 |
| T05 | `valid_until < generated_at` | validator 실패 |
| T06 | `schema_version: "1.0"` | validator 실패 (deprecated) |
| T07 | strategy_tag `"RSI_REBOUND"` | validator 실패 (폐기) |
| T08 | scale_out portion 합 1.01 | validator 실패 |
| T09 | ticker universe 밖 | Scout 단계에서 필터 |
| T10 | 동일 ticker 중복 발행 | 두 번째 거부 |
| T11 | entry.valid_until > sheet.valid_until | validator 실패 |
| T12 | `size.final_pct: 0.003` (MIN 미달) | Strategy Engine 미발행 |
| T13 | manual provenance | 정상 발행, meta Eval 제외 |
| T14 | schema_version MAJOR 불일치 | Executor 시트 거부, observer 이벤트 |
| T15 | profit_floor 활성화 후 floor 깨짐 | 시장가 전량 청산, exit_reason="profit_floor" |
| T16 | profit_floor activate 전 하락 | 미발동, 다음 rule 평가 |
| T17 | profit_floor + trailing_tp 동시 존재, profit_floor가 먼저 매칭 | profit_floor 청산 (배열 순서) |
| T18 | death_cross 발생하나 수익 구간 | 미발동 (min_loss_pct 미충족) |
| T19 | death_cross 발생 + -1.5% 손실 | 시장가 전량 청산, exit_reason="death_cross" |
| T20 | death_cross와 fixed_sl 동시 조건 | death_cross 먼저 매칭 (배열 순서) |

Edge case 시나리오(§8)는 Executor 통합 테스트로 별도 커버.

---

**문서 끝.**
