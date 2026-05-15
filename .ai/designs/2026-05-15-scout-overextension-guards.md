# Scout Overextension Guards + Outcome Feedback Design (2026-05-15)

> 본 문서는 `2026-05-15-cooldown-and-duplicate-guard.md` (audit B1/B2/C fix) 의
> 후속. 같은 사고 (5-15 오전 -3.35M 손실) 의 **상위 원인 — scout 의 추천 패턴**
> 자체를 다룬다. cooldown 가드는 "손절 직후 재진입" 을 막을 뿐, **"애초에 상투
> 종목을 추천하는 구조"** 는 그대로 둠.

## 1. Background — 추출된 데이터 (5-15 손절 10종목)

### 1.1 어제(5-14) 종가까지 의 overextension 지표

| ticker | 5d 누적 | 20d 누적 | 60d 고가 대비 | 20d 고가 대비 |
|---|---|---|---|---|
| 001440 | **+23.97%** | +28.01% | 92.04% | 92.04% |
| 003670 | +7.82% | +17.72% | **98.95%** | 98.95% |
| 006260 | +12.27% | +8.57% | 94.96% | 94.96% |
| 009540 | +6.47% | +3.71% | 95.56% | **99.41%** |
| 012330 | +6.09% | +6.62% | 95.53% | 98.50% |
| 028050 | -3.22% | **+40.37%** | 90.92% | 90.92% |
| 062040 | **+29.06%** | +24.43% | **97.83%** | 97.83% |
| 079550 | -4.89% | +32.53% | 89.48% | 89.48% |
| 329180 | +8.21% | -8.05% | 84.12% | 95.36% |
| 402340 | **+20.07%** | +12.17% | **97.71%** | 97.71% |

핵심: **10건 중 9건이 60일 고가 89% 이상, 5건이 95% 이상**. 단순 룰 3종으로 8건 차단 가능 (§4 참조).

### 1.2 Scout 의 반복 추천 패턴

`position_sheets` 추출 결과:

| ticker | 5-13~5-15 시트 발행 횟수 |
|---|---|
| 003670 | **9회** |
| 001440 | 7회 |
| 009540 | 7회 |
| 006260 | 6회 |
| 079550 | 5회 |
| 기타 5종목 | 1~3회 |

가설 문구가 거의 동일:
> "KOSPI 강세 + 반도체/IT 섹터 모멘텀(+36%) + 실적 호재 뉴스 + 리스크 이벤트 없음"

5-14 cron 마다 (08:30/09:30/10:30/12:30/13:30/14:30) 동일 패턴 반복 → 같은 종목 시트 다중 발행 → 다중 진입. 이번 세션의 B1 fix (`today_entries`) 가 어제 있었으면 다중 진입 차단됐을 패턴.

### 1.3 Outcome 피드백 부재의 증거

오늘(5-15) 11:10 scout run 이 003670, 006260 을 **또 추천**. 같은 종목이 오늘 09:00~11:13 사이 손절됐는데도. → Scout 가 자기 직전 run 의 결과를 모르는 결정적 증거.

## 2. Root Cause — 4 종 구조적 결함

| # | 결함 | 위치 | 오늘 손실 기여 |
|---|---|---|---|
| (a) | 입력 데이터 = 어제 일봉 60일 뿐 → 시초 갭다운/약세 모름 | `slow_loop/scout/market_data_loader.py` | 4건 (오늘 09:00~10:00 신규 진입) |
| (b) | 평가 방향: 모멘텀만, "이미 너무 오른 것" 회피 가이드 없음 | `slow_loop/scout/prompts.py` SCOUT_SYSTEM_PROMPT | 10건 전부 (상위 원인) |
| (c) | Outcome 피드백 부재 — 자기 추천 결과 모름 | `slow_loop/scout/context_builder.py` 의 ScoutContext | 8건 (5-14 진입분 — 같은 종목 5-14 ~ 5-15 반복) |
| (d) | 동일 종목 24h 반복 추천 차단 어제 부재 | `slow_loop/scout/...` (이번 세션 B1 fix 로 today_entries 노출) | 5-14 다중 진입 |

(d) 는 이번 세션에서 해결. 본 design 은 (a)(b)(c) 를 다룬다.

## 3. Five-Layer Guards — Layer 배치

| # | 가드 | Layer | 의사결정에 작용 | 막는 손실 패턴 |
|---|---|---|---|---|
| **G1** | Outcome 피드백 | Scout context + Strategy Engine | scout 정보 노출 + 손절율 높은 tag 의 시트 차단 | 같은 패턴 반복 |
| **G2** | Overextension validator | **Strategy Engine sheet 발행 단계** | scout 가 추천해도 시트 발행 차단 | 상투 종목 (10건 중 8건) |
| **G3** | 시초 갭다운 entry 가드 | **fast_loop entry path** | 시초가 갭다운 시 entry 보류 | 신규 시초 진입 (오늘 4건) |
| **G4** | 시초 추격 손절 timing 변경 | **fast_loop exit_evaluator** | 시초 5분 손절 보류 (변동성 흡수) | 어제 진입 → 오늘 시초 갭다운 손절 (오늘 8건) |
| **G5** | today_exit_cooldown (2026-05-15 후속) | **Strategy Engine sheet 발행 단계** | 같은 거래일 청산 (익절/손절 무관) 시 reject | 손절→재진입→손절 11건 + 익절→재진입→손절 2건 |

핵심 원칙 (사용자 정립 `feedback_prompt_control_limit`):
- **모든 enforcement 는 결정론 코드 layer**. Scout prompt 변경은 정보 노출만, 차단은 후행 layer.
- **외톨이 패턴 금지** (`project_audit_2026_05_14`). G2 는 strategy_engine, G3/G4 는 fast_loop — 각자 자기 위치에서 동작.

## 4. 임계값 근거 (보수 시작 → calibration)

### G2 Overextension Validator

| 룰 | 임계값 (보수) | 오늘 차단 종목 |
|---|---|---|
| 60일 고가 대비 | `close / high_60d > 0.95` → reject | 003670, 009540, 012330, 062040, 402340 (5건) |
| 20일 누적 수익률 | `r20d > 0.25` → reject | 028050(+40), 079550(+32) (+2건) |
| 5일 누적 수익률 | `r5d > 0.20` → reject | 001440(+24), 062040(+29) (중복 1, 신규 1건) |

세 룰 OR — 오늘 10건 중 **8건 차단**. 안 막힘: 006260 (60d 고가 대비 94.96%, 임계값 95% 직전), 329180 (84.12% 로 가장 낮음 — 다른 원인 추정).

calibration plan:
- Phase 0 #2 (`project_vision_llm_autonomy_dropped`) 데이터셋이 baseline.
- 1주 운영 후 reject 횟수 / 매매 0 risk 측정. 매매가 과도하게 막히면 임계값 완화 (0.95 → 0.97, 0.25 → 0.30).

### G3 시초 갭다운 entry 가드

| 룰 | 임계값 (보수) |
|---|---|
| 시초 갭 | `open / prev_close - 1 < -0.03` → entry 보류 + 큐 유지 |
| 09:30 전 시초 모멘텀 | KIS snapshot.current_price < open × 0.99 → entry 보류 |

calibration: 5-15 시초 갭다운 폭 실측치 ground truth 로 사용 (오늘 손절 10건의 5-15 시초가 vs 5-14 종가 비교).

### G4 시초 추격 손절 timing

| 룰 | 임계값 (보수) |
|---|---|
| 09:00~09:05 신규 손실 진입 | 즉시 손절 보류, 09:05 까지 관찰 후 재평가 |
| 손절 후 5분 내 반등 측정 | 별도 로깅 (calibration 데이터 누적) |

근거: `trading-domain.md` "5-14 5건 손절 중 4건 손절 후 자연 반등 (+0.96~+4.92%)". Phase 0 #2.

## 5. 도입 순서 + 오늘 범위

### 오늘 (5-15) 실제 도입 — 장중 emergency 포함

**KST 13:25 ~ 14:00 동안 5 commit 일괄:**
1. `aefda5a` fix(guards): metadata key 'reason' → 'exit_reason' — 이번 세션 cooldown 가드
   5종이 모두 잘못된 키 사용 → 5-15 부터 가드 무용지물 사실 발견 후 hotfix.
2. `f4b5679` feat(scout) G1: scout_outcomes_v1 view + ScoutContext.previous_outcomes +
   prompt v0.5 → v0.6 (outcomes 섹션).
3. `b895d78` fix(scout): G1 SQL asyncpg syntax (:days::int → make_interval).
4. `048b915` feat(guards): **today_exit_cooldown (G5)** — 5월 첫 주 데이터 (손절→재진입
   11건 + 익절→재진입→손절 2건 vs 회복 2건, net +48%p) 근거. prompt v0.6 → v0.7.

**검증 결과 (직접 호출):**
- `_fetch_today_exits` → 12 ticker 정확 검출 (오늘 손절 10 + 익절 011070 + manual 128940)
- `PgActiveSheetChecker.has_recent_exit_today` → True/False 정확 판정
- 14:30 cron 은 `macro_closed` 라 scout 미실행 — 자연 검증은 5-18 시초로 이월

### 다음 거래일 (5-18 월) 시초 자연 검증
- 어제 (이번 세션) cooldown 가드 효과 측정.
- G1 데이터 흐름 정상 동작 확인.

### 5-18 ~ 5-22 동안
- **G2 overextension validator** — strategy_engine sheet 발행 단계에 추가. 임계값 보수 시작.
- 1주 측정: reject 횟수, 매매 빈도 변화, 손절율 변화.

### 5-25 ~
- **G3 시초 갭다운 entry 가드** — fast_loop entry path. KIS snapshot 의존 추가.
- **G4 시초 추격 손절 변경** — Phase 0 #2 데이터 (1~2주 누적) 후 결정.

각 단계 끝 measure → adjust → next. 절대 동시 도입 금지.

## 6. 위험 / 트레이드오프

| 위험 | 완화 |
|---|---|
| 가드 너무 강해서 매매 0 | 단계적 도입, 첫 임계값은 보수. 1주 측정 후 완화. |
| G2 reject 후 그 종목이 실제로 더 오르는 기회비용 | 측정만. 보수적 임계값은 "정말 명백한 상투" 만 차단. 1주 후 reject 종목 사후 수익률 추적. |
| G1 prompt 추가로 토큰 비용 증가 | previous_outcomes 는 최근 7일 만, ticker 별 1줄 summary. < 500 토큰. |
| G3 시초 갭다운 회피로 진짜 갭상승 종목 진입 누락 | 보수 임계값 (-3%) — 갭상승 종목은 영향 없음. 갭다운만 차단. |
| G4 시초 손절 보류로 큰 손실 확대 | 5분 한정 + 09:05 재평가. 본격 손실 구간 (-7% 이하) 은 즉시 손절 유지. |

## 7. 오늘 구현 상세 (G1 only)

### 7.1 migrations/019_scout_outcomes_view.sql

```sql
CREATE OR REPLACE VIEW scout_outcomes_v1 AS
SELECT 
  ps.scout_run_id := (ps.provenance_json->>'scout_run_id') AS scout_run_id,
  ps.sheet_id,
  ps.ticker,
  ps.strategy_tag,
  (ps.sheet_json->'sizing'->>'final_pct')::numeric AS final_pct,
  ps.generated_at,
  e_buy.executed_at AS bought_at,
  e_buy.average_price AS entry_price,
  e_sell.executed_at AS exit_at,
  e_sell.average_price AS exit_price,
  (e_sell.metadata_json->>'reason') AS exit_reason,
  CASE 
    WHEN e_buy.average_price IS NOT NULL AND e_sell.average_price IS NOT NULL
    THEN ROUND(((e_sell.average_price / e_buy.average_price) - 1)::numeric, 4)
    ELSE NULL
  END AS pnl_pct
FROM position_sheets ps
LEFT JOIN executions e_buy ON e_buy.sheet_id = ps.sheet_id AND e_buy.side = 'buy'
LEFT JOIN executions e_sell ON e_sell.sheet_id = ps.sheet_id AND e_sell.side = 'sell'
WHERE ps.generated_at > now() - interval '30 days';

GRANT SELECT ON scout_outcomes_v1 TO pj_runtime;
```

(syntax 는 작성 시 검증 — `:=` 는 PL/pgSQL 전용이므로 `AS` 로 수정 필요)

### 7.2 ScoutContext 확장

`schemas.py`:
```python
@dataclass(frozen=True)
class PreviousOutcome:
    ticker: str
    strategy_tag: str
    generated_at: datetime
    exit_reason: str | None
    pnl_pct: Decimal | None

@dataclass(frozen=True)
class ScoutContext:
    # ... 기존 필드
    previous_outcomes: list[PreviousOutcome] = field(default_factory=list)
```

`context_builder.py` 에 `_fetch_previous_outcomes(engine, days=7) -> list[PreviousOutcome]` 추가. fail-open (조회 실패 시 빈 list).

### 7.3 prompts.py 정보 노출

`build_user_prompt` 에 섹션 추가:

```
## ⚠️ 직전 7일 추천 outcomes (학습 신호 — 같은 패턴 반복 금지)
- 손절율: 18/24 = 75% (SECTOR_MOMENTUM 12/14, EARNINGS_DRIFT 5/8, GAP_UP_REBOUND 1/2)
- 평균 PnL: -2.66%
- 최근 손절 ticker: 009540 -5.17, 001440 -3.96, 062040 -4.17, ...

위 데이터는 informational. **enforcement 는 후행 layer** 가 담당.
```

prompt 톤은 `feedback_prompt_control_limit` 원칙대로 강제 명령 아님 — informational. 실제 차단은 G2 (다음 단계) 에서.

### 7.4 테스트

- `tests/slow_loop/scout/test_context_builder.py` — 기존 today_entries / recent_stops fail-open 테스트 패턴 따름.
- 신규: `test_previous_outcomes_fetch` (engine None / 빈 결과 / 정상 결과 / DB 오류 fail-open).
- 신규: `tests/slow_loop/scout/test_prompts_outcomes_section.py` — prompt 에 outcomes 섹션 포함 확인.

### 7.5 배포 (15:30 이후)

```bash
# 1. migration 수동 적용 (deploy 가 자동 적용 안 함 — 직전 세션 학습)
ssh prime-jennie "docker exec -i prime-jennie-runtime-postgres-1 psql -U pj_runtime -d prime_jennie_v3" \
  < migrations/019_scout_outcomes_view.sql

# 2. git push → MS-01 runner 자동 배포
git push origin main

# 3. slow-loop 재기동 확인 (자동) → 다음 scout run (5-18 08:30) 에서 자연 활용
ssh prime-jennie "docker logs prime-jennie-runtime-slow-loop-1 --tail 50"
```

## 8. Open Questions

- **G1 prompt 정보 노출이 scout 행동을 실제로 바꾸는가?** — LLM 자가 검열 효과 측정 불가. G2 가 본질이고 G1 은 데이터 흐름 마련 + scout 의 행동 변화 관찰용.
- **G2 임계값 calibration cadence** — 1주 단위 vs 매일. 첫 1~2주는 매일 측정 → 안정되면 주간.
- **scout 가 동일 종목 반복 추천하는 근본 원인** — input 이 거의 동일하기 때문. (b) 결함은 prompt 변경으론 한계, 입력 데이터 다변화 또는 출력 후처리 (G2) 가 정공법.
- **006260, 329180 처럼 임계값 직전/낮은 overextension 종목** — 다른 원인 (뉴스 노이즈? 섹터 가중?). G1 outcome 데이터 누적 후 별도 분석.

## 9. 참조

- 직전 cooldown design: `.ai/designs/2026-05-15-cooldown-and-duplicate-guard.md`
- audit baseline: `project_audit_2026_05_14.md`
- Phase 0 선결과제: `project_vision_llm_autonomy_dropped.md` (#1 conviction-outcome correlation = G1, #2 5% SL 진단 = G4)
- 코드 위치:
  - `prime_jennie_runtime/slow_loop/scout/context_builder.py`
  - `prime_jennie_runtime/slow_loop/scout/schemas.py`
  - `prime_jennie_runtime/slow_loop/scout/prompts.py`
  - `prime_jennie_runtime/slow_loop/scout/market_data_loader.py`
  - `prime_jennie_runtime/slow_loop/strategy/engine.py` (G2 향후)
  - `prime_jennie_runtime/fast_loop/consumer.py` (G3 향후)
  - `prime_jennie_runtime/fast_loop/exit_evaluator.py` (G4 향후)
