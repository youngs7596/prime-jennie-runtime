# G2 Overextension Validator Design (2026-05-17)

> **⚠️ DEPRECATED 2026-05-17** — Pre-flight (`.ai/analyses/2026-05-17-g2-thresholds-historical-validation.md`)
> 결과 임계값이 손익 구분 못함 (익절 80% > 손절 70% 차단). 가격 기반 지표는
> outcome 예측력 없음 학습. 공식 폐기 결정: `.ai/designs/2026-05-17-g-series-simplification.md`
> §3. hold 측면은 `thesis_aware_hold` (구 G6) catalog 가 흡수. entry 측면은
> future backlog.
>
> 아래 본문은 archive — 결정 학습 산출물로 보관.
>
> ---
>
> 본 문서는 `2026-05-15-scout-overextension-guards.md` (G1~G5 통합 design) 의
> 후속. G2 (overextension validator) 만 깊게 다룬다. G1/G5 는 5-15 도입 완료,
> G2 는 5-18 자연 검증 후 구현 예정 — 본 문서는 그 사전 준비.
>
> 핵심 원칙 (`feedback_prompt_control_limit`): scout prompt 의 informational
> 노출(G1) 만으론 부족, **결정론 코드 layer 에서 차단**(G2) 한다.

## 1. Position in 5-Layer Guards

| # | 가드 | Layer | 상태 |
|---|---|---|---|
| G1 | Outcome 피드백 | Scout context + prompt | **DONE** (5-15) |
| **G2** | **Overextension validator** | **Strategy Engine sheet 발행** | **구현 보류 (2026-05-17 Pre-flight 부정적)** — [analyses](../analyses/2026-05-17-g2-thresholds-historical-validation.md) |
| G3 | 시초 갭다운 entry 가드 | fast_loop entry path | future |
| G4 | 시초 추격 손절 timing | fast_loop exit_evaluator | future (Phase 0 #2 후) |
| G5 | today_exit_cooldown | Strategy Engine sheet 발행 | DONE (5-15) |

G2 는 G5 (`build_sheet_with_reason` 3c) 바로 다음 **3d 단계** 에 추가.

## 2. 데이터 의존성 — daily_prices 60일

### 2.1 ScreeningCandidate 는 가격 데이터를 보유하지 않음

- `ScreeningCandidate` (slow_loop/scout/schemas.py:193) 필드: ticker / strategy_tag
  / conviction / entry_hint / exit_hint / factors / notes — **가격 없음**.
- `factors: dict[str, float]` 에 scout LLM 이 채울 수도 있으나, 결정론 차단을
  LLM 산출물에 의존하는 것은 `feedback_prompt_control_limit` 위반 (LLM 비결정성
  으로 edge case 새어나옴).
- 따라서 Strategy Engine 이 별도 fetch.

### 2.2 fetch 대상 — daily_prices

`migrations/003_price_tables.sql`:
```
daily_prices (stock_code TEXT, price_date DATE, open/high/low/close_price INT,
              volume BIGINT, PRIMARY KEY (stock_code, price_date))
INDEX ix_daily_prices_date (price_date)
```

PK index 로 ticker + date 조회 매우 cheap. candidate 별 1회 호출 가정 (보통
scout run 당 5~10건).

### 2.3 fetch SQL (단일 ticker, 60일)

```sql
WITH recent AS (
    SELECT close_price, high_price, price_date,
           ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
    FROM daily_prices
    WHERE stock_code = :ticker
      AND price_date <= :as_of_date
    ORDER BY price_date DESC
    LIMIT 60
)
SELECT
    MAX(close_price) FILTER (WHERE rn = 1)  AS latest_close,
    MAX(high_price)                          AS high_60d,
    MAX(close_price) FILTER (WHERE rn = 5)  AS close_5d_ago,
    MAX(close_price) FILTER (WHERE rn = 20) AS close_20d_ago
FROM recent;
```

(syntax 는 구현 시 정확히 검증 — asyncpg/SQLAlchemy text 양쪽 호환.)

반환 4 값으로 3개 비율 계산:
- `close_to_high_60d = latest_close / high_60d`
- `r5d = latest_close / close_5d_ago - 1`
- `r20d = latest_close / close_20d_ago - 1`

NULL 시 (60일 미만 또는 row 부족) fail-open — 차단 안 함.

### 2.4 Fail 정책 (상황별 — G6 §5.5 와 일관)

단일 fail-open 은 enforcement layer 무력화 위험 (G6 self-critique 학습). 상황별
분리 정책:

| 상황 | 정책 | 근거 |
|---|---|---|
| (A) row 부족 (60일 미만 daily_prices) | **차단 안 함 (fail-open)** + log debug | 정상 상황. 신규 상장 ticker 또는 ETL backfill 진행 중. 매매 진행 안전. |
| (B) SQL 오류 / 비율 계산 NaN | **차단 안 함 (fail-open)** + log warning | code-level bug. 운영 영향 최소화. 즉시 알림은 alert 채널로. |
| (C) DB 장애 / connection timeout | **차단 + telegram 알림 (fail-loud)** + 다음 cron 까지 sheet 발행 지연 | 다른 가드 (G5 등) 도 동시 실패 가능. 매매 진행은 위험. G6 §5.5 (B) 와 동일 사상. |
| (D) 정상 평가 + 임계값 초과 | **차단 (`overextension_*` reason)** | 본 design 핵심 동작 |
| (E) 정상 평가 + 임계값 미만 | 통과 | 본 design 정상 path |

**(C) 와 다른 가드의 일관성**: PgActiveSheetChecker 의 현재 정책은 단순
fail-open. 본 design 의 (C) 정책 도입 시 PgActiveSheetChecker 도 동일 패턴
이관 검토 (별도 PR, follow-up).

## 3. Strategy Engine 통합

### 3.1 새 Protocol — `OverextensionChecker`

`ActiveSheetChecker` 에 method 추가 vs 별도 Protocol. **별도 Protocol** 채택
이유:
- 단일 책임 (cooldown vs overextension 은 독립 도메인)
- 테스트에서 mock 분리 용이
- fail-open 정책 / SQL 분리

```python
@runtime_checkable
class OverextensionChecker(Protocol):
    async def is_overextended(
        self,
        ticker: str,
        strategy_tag: str,
        as_of_date: datetime,
    ) -> tuple[bool, str | None]:
        """True 면 차단, 두번째는 어느 룰 트리거인지 (로깅용)."""
        ...
```

`NullOverextensionChecker` 는 항상 `(False, None)` — fallback.

### 3.2 `PgOverextensionChecker`

```python
@dataclass(frozen=True)
class OverextensionThresholds:
    close_to_high_60d: float  # 0.95 default
    r20d: float               # 0.25 default
    r5d: float                # 0.20 default

DEFAULT_THRESHOLDS = OverextensionThresholds(
    close_to_high_60d=0.95, r20d=0.25, r5d=0.20,
)

# strategy_tag 차등 (§4.2). sample 부족 tag 는 default 사용.
THRESHOLDS_BY_TAG: dict[str, OverextensionThresholds] = {
    "EARNINGS_DRIFT": OverextensionThresholds(0.90, 0.20, 0.15),
    "SECTOR_MOMENTUM": DEFAULT_THRESHOLDS,
}

# MEAN_REVERT_RSI 는 본 가드 적용 제외 (§5)
SKIP_TAGS = frozenset({"MEAN_REVERT_RSI"})


class PgOverextensionChecker:
    def __init__(
        self,
        engine,
        *,
        thresholds_by_tag: dict[str, OverextensionThresholds] | None = None,
        default_thresholds: OverextensionThresholds = DEFAULT_THRESHOLDS,
        skip_tags: frozenset[str] = SKIP_TAGS,
    ) -> None:
        ...

    async def is_overextended(
        self, ticker: str, strategy_tag: str, as_of_date: datetime,
    ) -> tuple[bool, str | None]:
        # §6 MEAN_REVERT_RSI early return (skip)
        if strategy_tag in self._skip_tags:
            return False, None

        # §2.3 fetch + 비율 계산
        latest_close, high_60d, close_5d, close_20d = await self._fetch(ticker, as_of_date)
        if latest_close is None or high_60d is None:
            return False, None  # §2.4 (A) row 부족 fail-open

        t = self._thresholds_by_tag.get(strategy_tag, self._default)

        # 세 룰 OR 평가 — 첫 trigger 즉시 반환 (DB roundtrip 후 in-memory)
        close_to_high = latest_close / high_60d
        if close_to_high > t.close_to_high_60d:
            return True, "close_to_high_60d"
        if close_5d and (latest_close / close_5d - 1) > t.r5d:
            return True, "r5d"
        if close_20d and (latest_close / close_20d - 1) > t.r20d:
            return True, "r20d"
        return False, None
```

핵심 명세:
- `THRESHOLDS_BY_TAG` 에 sample 충분한 tag (EARNINGS_DRIFT, SECTOR_MOMENTUM) 만
  명시. **GAP_UP_REBOUND / MEAN_REVERT_RSI 는 §4.2 약점 #2 정정에 따라 default
  적용 또는 skip**.
- `SKIP_TAGS` 명시 — MEAN_REVERT_RSI 의 §5 구조적 의미 충돌 (§6 약점 정정).
- `thresholds_by_tag` 는 guard-specific config — yaml policy 와 무관, code-level
  dataclass.
- `is_overextended` 의 OR 평가는 첫 trigger 즉시 반환 (성능 + 명확한 rule 식별).

### 3.3 `build_sheet_with_reason` 3d 단계 추가

`engine.py:436` (3c 직후) 에 삽입:

```python
# 3d. Overextension validator (2026-05-18 도입) — design 2026-05-17-g2.
# 60d 고가 95% / 20d +25% / 5d +20% OR — 5-15 데이터로 8/10 차단 검증.
# strategy_tag 별 차등 임계값 (EARNINGS_DRIFT 더 엄격).
is_overextended, rule = await self._overextension_checker.is_overextended(
    candidate.ticker, tag, inputs.generated_at,
)
if is_overextended:
    logger.info(
        "sheet_rejected: overextension ticker=%s tag=%s rule=%s",
        candidate.ticker, tag, rule,
    )
    return None, f"overextension_{rule}"
```

rejection_reason 코드: `overextension_close_to_high_60d` /
`overextension_r20d` / `overextension_r5d`. screening_candidates 컬럼에 직접
저장 → 1주 후 reject 분포 분석 가능.

### 3.4 app.py 주입

`slow_loop/app.py:288` 근처:
```python
if db_engine is not None:
    from prime_jennie_runtime.slow_loop.strategy.engine import (
        PgActiveSheetChecker, PgOverextensionChecker,
    )
    active_checker = PgActiveSheetChecker(db_engine)
    overextension_checker = PgOverextensionChecker(db_engine)
else:
    active_checker = None
    overextension_checker = None

engine = StrategyEngine(
    policy=policy,
    risk_throttle=risk_throttle,
    active_checker=active_checker,
    overextension_checker=overextension_checker,
)
```

## 4. 임계값

### 4.1 시작점 (보수, default)

| 룰 | 임계값 | 의미 |
|---|---|---|
| close_to_high_60d | `> 0.95` → reject | 60일 고가의 95% 이상 |
| r20d | `> 0.25` → reject | 20일 누적 +25% 이상 |
| r5d | `> 0.20` → reject | 5일 누적 +20% 이상 |

세 룰 **OR**. 5-15 손절 10건 → **8 차단** (003670, 009540, 012330, 062040,
402340 / 028050 / 079550 / 001440). 안 막힘: 006260 (94.96%, 임계값 직전), 329180.

### 4.2 strategy_tag 별 차등 (G1 outcomes sample 근거)

handoff 5-15-0003 §G1 발췌 — 1주 outcomes 의 strategy_tag 별 손절율:

| strategy_tag | sample | 손절율 | 통계 의미 | 차등 적용 |
|---|---|---|---|---|
| EARNINGS_DRIFT | 14/16 | **87.5%** | ✓ 충분 | **엄격화** |
| SECTOR_MOMENTUM | 19/29 | 65.5% | ✓ 충분 | default |
| GAP_UP_REBOUND | 2/4 | 50% | ❌ n=4 무근거 | **default 적용** (Phase 후 재검토) |
| MEAN_REVERT_RSI | 1/1 | 100% | ❌ n=1 무근거 | **skip** (§5 구조적 의미 충돌) |

차등 임계값 표 (sample 충분한 tag 만):

| strategy_tag | close_to_high_60d | r20d | r5d | 근거 |
|---|---|---|---|---|
| **EARNINGS_DRIFT** | **> 0.90** | **> 0.20** | **> 0.15** | 87.5% 손절율 — 가장 엄격 |
| SECTOR_MOMENTUM (= default) | > 0.95 | > 0.25 | > 0.20 | 표준 |
| GAP_UP_REBOUND | (default) | (default) | (default) | sample 부족, 1주 후 차등 재검토 |
| MEAN_REVERT_RSI | (skip) | (skip) | (skip) | 구조적 의미 충돌 (§5) — `is_overextended` early return |

**약점 #2 정정**: 이전 design 의 GAP_UP_REBOUND 0.97/0.30/0.25 "완화" 는 sample 4
의 추정 직관, 통계 무근거. 정직하게 default 적용 + Phase 후 sample 누적 (target
n ≥ 20) 후 차등 도입 결정.

운영 1주 후 calibration (§6.3) 결과로 차등 표 갱신.

## 5. MEAN_REVERT_RSI 의 특수성

MEAN_REVERT_RSI 는 **이미 하락한 종목** 의 평균회귀 진입. "60일 고가 대비
가까움" 은 오히려 진입 시그널 (반등 안 한 상태). 본 가드의 overextension
정의가 반대 방향이라 **G2 적용 제외** 가 안전한 시작점.

대안 — MEAN_REVERT_RSI 만의 **언더-extension** 가드 (별도 design 후보, G7+ 가칭.
G6 은 thesis-aware exit 으로 점유 — `.ai/designs/2026-05-17-g6-thesis-aware-exit.md`):
- close_to_low_60d < 1.05 → reject (60일 저점 5% 이내, 추가 하락 위험)
- RSI 미회복 (RSI < 25) → reject

본 문서 범위 외. G2 v1 은 MEAN_REVERT_RSI 에 대해 `is_overextended() → (False, None)`
반환 (skip).

## 6. Rollout / Measurement

> **⚠️ 2026-05-17 update — 본 §6 일정 전체 보류**.
> Pre-flight (§7.4) 실행 결과 임계값이 손익 구분 못함 (익절 차단 80% >
> 손절 차단 70%). [analyses doc](../analyses/2026-05-17-g2-thresholds-historical-validation.md)
> 의 §7 옵션 (지표 재설계 / 범위 축소 / G6 흡수 / 폐기) 별도 세션 결정 후
> 본 §6 일정 재가동.

### 6.1 도입 순서 (보류)

1. **5-18 ~ 5-22** — G1/G5 자연 검증 + outcomes 데이터 1주 누적 (§10 5종 TODO).
2. **5-22 (금) 종료** — §4.2 차등 표 G1 1주 데이터로 재산정.
3. **5-23 ~ 5-24 (토일)** — **§7.4 Pre-flight 실행 (필수 gate)**. 평상 day 차단률 > 50% 시 §6.1 4 보류, 임계값 완화 PR 먼저.
4. **5-25 (월) 15:30 이후** — Pre-flight 통과 시 G2 구현 + deploy. 첫 임계값 §4.1+§4.2 (보수).
5. **5-25 ~ 5-29** — reject 횟수 / 매매 빈도 / 거부 종목 사후 수익률 측정 (§6.2 query).
6. **6월 첫 주 ~** — §6.3 weekly cron 시작. §9 OR/AND trigger 측정. 임계값 조정은 2주 연속 trigger 기반.

장중 deploy 금지 룰 준수 — 5-25 (월) 15:30 이후 또는 5-24 (일).

**§10 5-18 자연 검증 의존성** 은 본 §6.1 의 1번 단계와 동일 내용 — §10 은 detail 만.

### 6.2 측정 지표

screening_candidates 테이블에 `rejection_reason` 저장 → 다음 query 로 일간
모니터링:
```sql
SELECT date_trunc('day', generated_at AT TIME ZONE 'Asia/Seoul') AS day,
       rejection_reason,
       count(*),
       array_agg(DISTINCT ticker) FILTER (WHERE rejection_reason LIKE 'overextension_%')
FROM screening_candidates
WHERE rejection_reason LIKE 'overextension_%'
GROUP BY 1, 2
ORDER BY 1 DESC;
```

거부 후 사후 수익률 측정 (1주 hold 가정):
```sql
WITH rejected AS (
    SELECT ticker, generated_at::date AS d, rejection_reason
    FROM screening_candidates
    WHERE rejection_reason LIKE 'overextension_%'
)
SELECT r.ticker, r.d, r.rejection_reason,
       p_then.close_price AS close_then,
       p_5d.close_price AS close_5d_after,
       (p_5d.close_price::float / p_then.close_price - 1) AS r5d_after
FROM rejected r
LEFT JOIN daily_prices p_then ON p_then.stock_code = r.ticker AND p_then.price_date = r.d
LEFT JOIN daily_prices p_5d ON p_5d.stock_code = r.ticker
    AND p_5d.price_date = (r.d + interval '7 days')::date
ORDER BY r.d DESC;
```

reject 종목들이 평균적으로 상승했으면 임계값 너무 엄격 (over-blocking). 평균
하락이면 임계값 적절.

### 6.3 strategy_tag 별 calibration cadence + 책임자

**약점 #7 정정** — 자동/수동 / 책임자 / 알림 명시:

| 항목 | 정책 |
|---|---|
| 주기 | **매주 월요일 09:00 KST** (시초 cron 전 1회) |
| 자동화 | **slow_loop weekly cron 추가** — 자동 query 실행 + telegram 결과 자동 post |
| 담당 | cron 자동 — 사람 수동 실행 부담 0 |
| 알림 형식 | `[G2 weekly] strategy_tag 별 손절율: EARNINGS_DRIFT 85%(-3%p), SECTOR_MOMENTUM 60%(-5%p), ...` |
| 임계값 재산정 트리거 | 손절율 변화 > 10%p **2주 연속** 시 (single-week noise 회피) → 사람이 design 갱신 PR |

자동 query (slow_loop/jobs/g2_weekly_report.py 신규, 또는 기존 weekly job 에 추가):
```sql
SELECT strategy_tag,
       count(*) FILTER (WHERE exit_at IS NOT NULL) AS exits,
       count(*) FILTER (WHERE exit_reason IN ('fixed_sl','stop_loss','breakeven_stop')) AS stops,
       round(100.0 * count(*) FILTER (WHERE exit_reason IN ('fixed_sl','stop_loss','breakeven_stop'))
             / NULLIF(count(*) FILTER (WHERE exit_at IS NOT NULL), 0), 1) AS stop_pct,
       round(avg(pnl_pct) * 100, 2) AS avg_pnl_pct
FROM scout_outcomes_v1
WHERE generated_at > now() - interval '14 days'
GROUP BY strategy_tag
ORDER BY exits DESC;
```

operational burden: 사람 = 결과 telegram 확인만. 임계값 변경은 정량 trigger 2주
충족 시 design 갱신 PR — review 흐름은 일반 코드 변경과 동일.

## 7. Tests

### 7.1 Unit (`tests/slow_loop/strategy/test_overextension_checker.py`)

- `test_pg_checker_fail_open_on_db_error` — DB 장애 시 `(False, None)`
- `test_pg_checker_returns_false_when_no_data` — row 0 시 `(False, None)`
- `test_pg_checker_blocks_above_high_60d_threshold` — fixture: latest=95, high_60d=100 → block
- `test_pg_checker_blocks_above_r20d_threshold` — fixture: r20d=+26% → block
- `test_pg_checker_blocks_above_r5d_threshold` — fixture: r5d=+21% → block
- `test_pg_checker_strategy_tag_differential` — EARNINGS_DRIFT 90% / SECTOR_MOMENTUM 95% 차등 검증
- `test_pg_checker_mean_revert_rsi_skipped` — MEAN_REVERT_RSI 는 항상 `(False, None)`
- `test_null_checker` — fallback 동작 확인

### 7.2 Engine integration (`tests/slow_loop/strategy/test_engine.py` 추가)

- `test_engine_rejects_overextended_candidate` — mock checker `True` 반환 → `(None, "overextension_close_to_high_60d")`
- `test_engine_priority_overextension_after_cooldowns` — 3a~3c 거부 케이스가 우선, 3d 는 그 다음 호출 확인 (cooldown 트리거 시 fetch 안 함)
- `test_engine_fail_open_when_checker_is_null` — NullOverextensionChecker 면 통과

### 7.3 Integration

`tests/integration/test_overextension_with_daily_prices.py` — daily_prices
fixture (실제 stock_code 90일치 mock) → SQL 호출 → 정확한 비율 계산 검증.

## 7.4 Pre-flight (도입 전 1회 작업) — 약점 #3 해결

> **✅ 2026-05-17 실행 완료**. 결과:
> [`.ai/analyses/2026-05-17-g2-thresholds-historical-validation.md`](../analyses/2026-05-17-g2-thresholds-historical-validation.md)
> — **부정적 (도입 보류 trigger 충족)**. 익절 차단률 80% > 손절 차단률 70%,
> SECTOR_MOMENTUM 익절 차단 85.7% 가 가장 큰 손해. G2 v1 임계값 + 지표 부적합.

### 7.4.1 원래 계획

**G6 §8.5 와 동일 사상**: 5-15 단일 사건 (KOSPI -4% CRITICAL day) 의존만으론
평상 day 차단률 검증 부족 → 평상 day 손절 sample 30~50건으로 over-blocking 사전 검증.

**작업 절차** (예상 2~3시간):

1. **지난 30일 손절 sample 추출**:
   ```sql
   SELECT ps.ticker, ps.generated_at::date AS entry_day,
          ps.strategy_tag, e_sell.metadata_json->>'exit_reason' AS reason
   FROM position_sheets ps
   JOIN executions e_sell ON e_sell.sheet_id = ps.sheet_id AND e_sell.side = 'sell'
   WHERE e_sell.metadata_json->>'exit_reason' IN ('fixed_sl','stop_loss','breakeven_stop')
     AND ps.generated_at > now() - interval '30 days'
   ORDER BY ps.generated_at DESC;
   ```
   예상 row 30~60건.

2. **각 ticker 의 entry_day 시점 overextension 지표 계산** (§2.3 SQL 적용):
   - close_to_high_60d / r20d / r5d 추출

3. **§4.1 + §4.2 임계값 적용 시 차단률**:
   - 전체 sample 차단률
   - day 별 차단률 분포 (5-15 같은 special day vs 평상 day 차이)
   - 평상 day 차단률 < 30% target. 초과 시 임계값 완화 후 재측정.

4. **차단되지 않은 손절 sample 분석**: G2 가 못 잡은 패턴 — G3 (시초 갭다운) 후보 등 별도 가드 design 인풋.

**산출물**: `.ai/analyses/2026-05-XX-g2-thresholds-historical-validation.md`

본 작업 완료 후 §3 구현 + §6 deploy. **평상 day 차단률 > 50% 시 도입 보류** —
임계값 완화 또는 OR → AND 전환 검토.

## 8. 위험 / 트레이드오프

| 위험 | 완화 |
|---|---|
| 모멘텀 종목 (상승 추세 강한) 진입 완전 차단 | §9 OR → AND 정량 trigger 2종 충족 시 전환. 단순 1주 직관 결정 X. |
| daily_prices 데이터 부족 (60일 미만) | §2.4 (A) fail-open (debug log) — 신규 상장 / ETL 정상. |
| daily_prices SQL 오류 / NaN | §2.4 (B) fail-open (warning log) — code-level alert. |
| **DB 장애 / connection timeout** | **§2.4 (C) fail-loud (telegram + 차단)** — G2 가드 무력화 회피. |
| EARNINGS_DRIFT 차등 임계값 (90%) 가 너무 엄격해 매매 0 | §6.3 weekly cron + 2주 trigger. screening_candidates rejection_reason 분포로 정량 판단. |
| candidate 마다 DB roundtrip (5~10건) | PK index hit, cheap (< 5ms/query). 평균 candidate > 20 측정 시 IN(...) batch 검토 (§9). |
| sheet 발행 → 매매 → 자연 차익 기회 손실 | §6.2 `r5d_after` query 로 사후 수익률 추적. §9 AND 전환 trigger (2) 와 동일 신호. |
| **5-15 단일 사건 의존 — 평상 day over-blocking** | **§7.4 Pre-flight 사전 검증. 평상 day 차단률 > 50% 시 도입 보류.** |

## 9. Open Questions

해결된 항목 (design v2 본문 통합):
- ~~fail-open 단일 정책~~ → §2.4 상황별 5종 (A/B/C/D/E)
- ~~sample 부족 차등~~ → §4.2 sample 충분 tag 만 차등, GAP_UP/MEAN_REVERT default 또는 skip
- ~~5-15 단일 사건 의존~~ → §7.4 Pre-flight (지난 30일 평상 day sample 검증)
- ~~calibration cadence 모호~~ → §6.3 weekly cron + 2주 연속 trigger
- ~~MEAN_REVERT skip 코드 path~~ → §3.2 `is_overextended` early return + `SKIP_TAGS`
- ~~차등 × OR 코드 sketch~~ → §3.2 OverextensionThresholds dataclass + is_overextended 구현

남은 Open:

**OR vs AND 전환 criteria** (정량 trigger 명시 — 약점 #4 보강):
- 시작: OR (보수)
- AND 전환 trigger (1주 측정 후): **둘 모두 충족 시**
  - (1) reject 비율 > 50% (sheet 발행률 < 50%)
  - (2) reject 종목 사후 5d 수익률 평균 > +1% (= G2 가 모멘텀 종목까지 차단 = over-blocking 증거)
- 위 미충족 시 OR 유지. 단일 룰 임계값 완화는 별도 (§6.3 weekly report 기반).

기타:
- **EARNINGS_DRIFT 87.5% 손절율의 근본 원인** — 본 가드로 해결되는가 vs strategy
  자체 폐기? 가드 도입 후에도 stop_pct 유지 시 별도 분석.
- **20d / 5d 누적 기준일 — 영업일 vs 달력일** — daily_prices 는 영업일만 row 존재
  하므로 ROW_NUMBER offset 은 영업일 기준. 표기는 "5/20 영업일" 로 명시.
- **여러 ticker batch fetch 최적화** — 현재 candidate 별 1 query. scout run
  당 평균 candidate 수 < 10 이라 batch 의 효용 적음. 평균 candidate > 20 측정 시 IN(...) batch 검토.
- **PgActiveSheetChecker 의 fail 정책 이관** — §2.4 (C) DB 장애 fail-loud 패턴을 cooldown 가드들도 동일 적용할지. 별도 follow-up PR.
- **rejection_reason 코드 정리** — migration 012 comment 가 outdated (G5 / G2 신규 코드 미포함). 코드 변경 X, comment 업데이트 PR 필요. 컬럼 자체는 TEXT (length 무제한, 검증 완료).
- **price_date <= as_of_date 의 의미** — scout run 이 09:00 이전이면 어제 일봉
  까지. 운영 cron 시각 (08:30 첫 발화) 과 맞물려 안전.

## 10. 5-18 자연 검증 의존성 (§6.1 단계 1 detail)

본 design 의 §4.2 차등 임계값은 G1 outcomes 1주 데이터에 근거하므로, **5-18~5-22
G1 정상 동작 확인이 G2 구현 선결조건** (§6.1 1번 단계). handoff TODO 5종:

1. recently_exited_today_tickers / recent_stop_loss_tickers 동작
2. scout prompt v0.7 의 outcomes 섹션이 LLM 입력에 들어가는지
3. LLM 행동 변화 관찰
4. sheet 발행 시 cooldown reject 자연 발화
5. G1 outcome 데이터 적재 (scout_outcomes_v1 view 정상 row)

5종 모두 정상 = §6.1 2번 단계 (§4.2 재산정) 진행 가능. 하나라도 실패 시 G2 일정
전체 보류 + 원인 분석.

## 11. 참조

- 직전 design: `.ai/designs/2026-05-15-scout-overextension-guards.md` (§3 G2 표 + §4 임계값 초안)
- session 핸드오프: `.ai/sessions/session-2026-05-15-0003.md`
- migration: `migrations/003_price_tables.sql` (daily_prices), `migrations/019_scout_outcomes_view.sql`
- 코드 위치:
  - `prime_jennie_runtime/slow_loop/strategy/engine.py` (3c → 3d 추가 + OverextensionChecker Protocol + Pg/Null 구현 + OverextensionThresholds dataclass)
  - `prime_jennie_runtime/slow_loop/app.py:288` (PgOverextensionChecker 주입)
  - `prime_jennie_runtime/slow_loop/jobs/g2_weekly_report.py` (신규 — §6.3 weekly cron)
  - `tests/slow_loop/strategy/test_overextension_checker.py` (신규 — §7.1)
  - `tests/slow_loop/strategy/test_engine.py` (test 추가 — §7.2)
  - `tests/integration/test_overextension_with_daily_prices.py` (신규 — §7.3)
  - `.ai/analyses/2026-05-XX-g2-thresholds-historical-validation.md` (§7.4 Pre-flight 산출물)
- 글로벌 메모리: `trading-domain.md` — Sheet 발행 단계 가드 표 (3종 → 4종 갱신 예정)
- 원칙: `feedback_prompt_control_limit` / `project_audit_2026_05_14` (외톨이 패턴 회피)
