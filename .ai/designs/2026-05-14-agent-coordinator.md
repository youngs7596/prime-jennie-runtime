# Agent Coordinator — Design 초안

**작성일**: 2026-05-14
**상태**: 초안 (검토 중)
**대체**: `.ai/sessions/session-2026-05-12-0001.md` 의 폐기된 LLM 자율성 비전
**근거**: 2026-05-14 v3 audit 결과 (6 영역 12 위험) — 메모리 [[project-audit-2026-05-14]]

---

## 0. 원칙

1. **Slow loop / Fast loop 의 timing 전제 유지** — 의사결정 천천히, 매수 빠르게. Coordinator 는 timing 을 바꾸지 않고 정보 흐름을 보강한다.
2. **agents 끼리 직접 소통 금지** — 항상 Coordinator 거침. 이를 통해 cross-cutting 의사결정이 한 자리에서 가능.
3. **점진 도입** — 처음에는 advisory mode (관찰만, 차단 안 함). 운영 데이터 누적 후 enforce mode 단계 전환.
4. **Pydantic schema 의 lifecycle 안전성을 책임** — 발행 → 소비 → 결과 → 피드백 의 시간축 인과관계를 Coordinator 가 보존.
5. **결정론 ↔ 비결정론 분리** — LLM agents (Scout, Macro) 의 비결정성을 prompt 로 제어하려 하지 않는다. minyoung-mah 학습: prompt 제약은 LLM 비결정론 때문에 반드시 edge case 로 새어 나온다. 대신 Coordinator 가 **결정론 코드 + Pydantic validator + Policy Engine** 으로 검증/필터/제약한다. agents 는 추천을 만들고, Coordinator 가 그 추천을 검증한다. 이 분리가 prompt-driven edge case 폭주를 차단한다.
   - 예: "Scout prompt 에 같은 ticker 반복 금지 명시" X → Coordinator 의 dedup policy 가 결정론적 차단 O
   - 예: "Engine prompt 에 macro_gate 재확인" X → Coordinator 의 macro re-check policy O
   - 예: "exit prompt 에 cooldown 고려" X → Coordinator 의 cooldown policy O

---

## 1. 문제 진단 (audit 요약)

현재 v3 의 매수 결정 path:

```
Scout 발행 → Engine 변환 → consumer 큐 적재 → tick conditions 평가 → sizer → executor → KIS
```

각 단계가 자기 책임만 검증한다. **"전체 시스템 상태를 종합한 의사결정"** 을 책임지는 자리가 없다.

이로 인해 발견된 외톨이 패턴 (각 컴포넌트가 모르는 것):

| 컴포넌트 | 모르는 것 |
|---|---|
| Scout | 본인이 1시간 전에 추천한 종목, 그게 매수됐는지, 손절됐는지 |
| Strategy Engine | 같은 거래일에 이미 발행한 시트 (가드 dead code) |
| fast_loop consumer | 손절 history, 최근 매도 history (보유 여부만 봄) |
| tick_loop | 다른 task 들이 살아있는지 (한 명만 죽어도 다 같이 죽음) |
| exit_evaluator | 같은 종목의 entry 큐 상태, 사용자가 외부에서 거래한 사실 |
| sizer | 같은 tick 에 동시 처리되는 다른 sheet 들의 sizing |
| 큐의 시트 | macro gate 가 발행 후 closed 로 바뀐 사실 |
| 부팅 시 sync check | 장중에 발생하는 외부 매수/매도 |

→ 정보 흐름이 단방향만 있고 (Scout → Engine → fast_loop → KIS), **사후 피드백 채널 없음**.

---

## 2. Coordinator 의 책임

Coordinator 는 **세 가지 layer** 를 제공한다:

### Layer A. State Hub
시스템 전체 state 의 단일 view. agents 가 의사결정 직전에 query.

내용:
- 현재 보유 (sheet 단위 + ticker 단위)
- pending entry queue (큐의 모든 시트 + 평가 reason)
- 최근 거래 history (오늘 + 최근 N일, 손절 분리)
- macro 현재 gate / level / multiplier
- risk throttle 현재 level
- control state (stop / pause / dryrun)
- 시스템 health (각 task heartbeat)

### Layer B. Decision Authority
의사결정 시점에 agents 가 Coordinator 에 "이 결정 OK?" query → Coordinator 가 State Hub + Policy 종합 → OK / NOT OK + 사유 반환.

대상 의사결정:
- 매수 (시트 → entry executor 직전)
- 매도 (exit evaluator → exit executor 직전, 단 forced_liquidation 제외)
- 시트 발행 (Scout/Engine → 큐 적재 직전)
- 정책 변경 (macro/risk multiplier 적용 직전)

### Layer C. Event Bus
모든 의사결정 + 체결 + 정책 변경 이벤트가 한 bus 통과. agents 가 subscribe 하고 자기 컨텍스트 update.

이벤트 종류:
- `sheet_published` (Engine → bus)
- `entry_decided` (Coordinator → bus, OK/NOT OK 모두)
- `entry_filled` / `entry_rejected` (executor → bus)
- `exit_decided` / `exit_filled` (executor → bus)
- `policy_changed` (macro/risk 변경 시)
- `external_event` (사용자 수동 거래 감지 시)

---

## 3. 컴포넌트 사이의 관계

```
                    ┌────────────────────────────┐
                    │    Agent Coordinator       │
                    │  ┌──────────────────────┐  │
                    │  │  Decision Context    │  │ ← State Hub
                    │  │  (positions, queue,  │  │
                    │  │   recent_exits,      │  │
                    │  │   macro, risk, ...)  │  │
                    │  └──────────────────────┘  │
                    │  ┌──────────────────────┐  │
                    │  │  Policy Engine       │  │ ← Decision Authority
                    │  │  (cooldown, dedup,   │  │
                    │  │   sizing limits,     │  │
                    │  │   macro re-check)    │  │
                    │  └──────────────────────┘  │
                    │  ┌──────────────────────┐  │
                    │  │  Event Bus           │  │ ← 양방향 채널
                    │  └──────────────────────┘  │
                    └────┬───────────────┬───────┘
                         │ query/publish │
        ┌────────────────┼───────────────┼────────────────┐
        │                │               │                │
   Slow loop        Slow loop       Fast loop        Fast loop
   (Macro)          (Scout/Engine)  (consumer/tick)  (executor)
        │                │               │                │
        └────────────────┴───────────────┴────────────────┘
              (agents 끼리 직접 통신 금지)
```

- **Slow loop / Fast loop 의 timing 변경 없음**. Slow loop 는 여전히 시간 단위, Fast loop 는 tick 단위.
- **agents 끼리 직접 소통 안 함**. Scout 가 fast_loop 의 보유를 알고 싶으면 Coordinator query. 매수 시 executor 가 macro 를 알고 싶으면 Coordinator query.
- Coordinator 는 long-running async service (별도 컨테이너 또는 fast_loop 안의 별도 task).

---

## 4. Decision Context 모델 (State Hub 의 schema)

매수 결정 시 Coordinator 에 전달되는 컨텍스트 예시:

```python
class DecisionContext(BaseModel):
    """매수 결정 시점의 전체 컨텍스트."""
    # 자기 자신
    sheet_id: str
    ticker: str
    strategy_tag: str
    conviction: float
    final_pct: float
    entry_price_hint: float | None

    # 보유 / 큐
    held_sheets: list[HeldSheetSummary]  # 같은 ticker 보유 / 다른 ticker 보유
    queued_sheets: list[QueuedSheetSummary]  # 같은 tick 동시 처리되는 시트들

    # 거래 history (시간축 인과)
    today_exits: list[ExitRecord]  # 오늘 손절/매도 + reason + pnl
    recent_exits_n_days: list[ExitRecord]  # 최근 N일
    today_entries: list[EntryRecord]  # 오늘 매수 이력

    # 정책 컨텍스트
    macro_gate: Literal["closed", "open"]
    macro_size_mult: float
    risk_level: Literal["NORMAL", "CAUTION", "WARNING", "DANGER", "CRITICAL"]
    risk_multiplier: float
    control_state: ControlStateSnapshot  # stop/pause/dryrun

    # 시스템 health
    kis_balance: int
    cash_available: int
    total_asset: int
    cached_balance_age_sec: float

    # 발행 컨텍스트 (timing 인과)
    sheet_generated_at: datetime
    sheet_age_sec: float  # 발행 후 경과
    scout_run_id: str
    macro_run_id: str  # 시트 발행 시점의 macro
```

→ Coordinator 의 Decision Authority 가 이 컨텍스트 위에서 정책 평가.

---

## 5. Policy Engine — 정책 예시

Coordinator 의 Policy Engine 이 검증하는 항목 (audit 12 위험 매핑):

| Audit ID | 위험 | Coordinator 의 처방 |
|---|---|---|
| B1 | Scout history blindness | State Hub 의 `today_entries` + `recent_exits` 를 Scout context 에 노출 → 발행 단계 자기검열 |
| B2 | Engine dedup dead code | Policy 의 "duplicate_today" 체크 (실제 활성화). 같은 거래일 같은 ticker 시트 있으면 NOT OK |
| C 변동성 흡수 | 손절 직후 같은 자리 재매수 | Policy 의 "recent_stoploss_cooldown" — 손절 후 X 시간 내 재매수 시 conviction 임계 상승 필요 |
| A1 | entry 무한 retry | Event Bus 의 `entry_rejected` 누적 → Policy 의 "max_rejected_per_sheet" 도달 시 큐에서 제거 |
| A2/D2 | 부분체결 잔량 drift | State Hub 의 `kis_balance` 와 `held_sheets` 정기 reconcile (장중 5분 주기) → 차이 발견 시 event_bus 발행 |
| D1 | 장중 외부 매수 미감지 | State Hub 의 reconcile 이 양방향 (only_in_state + only_in_kis) 자동 |
| E1/E2 | task die → 전체 죽음 | Coordinator 의 supervisor — task heartbeat 감시, die 한 task 만 restart |
| F1 | cached balance race | sizer 가 Coordinator query → State Hub 의 `cash_available` 가 동시 처리 sheet 들의 commit 누적 반영 |
| F2 | risk_throttle 중복 | Decision Authority 가 `final_pct` 와 `risk_multiplier` 의 중복 여부 인지 → 한 번만 적용 |
| F6 | macro gate transition race | 매수 직전 Policy 가 macro 재확인 (state hub 의 현재 macro_gate) |

→ **12 위험 모두 개별 fix 없이 Coordinator 의 3 layer 안에서 해결**.

---

## 6. minyoung-mah 와의 관계

- **minyoung-mah 의 `Orchestrator`** (`../minyoung-mah/minyoung_mah/`): 단일 agent 의 tool calling loop 추상화. 한 LLM 호출 안에서 role 정의 + tools + output_schema.
- **v3 Coordinator**: multi-service mesh 의 coordination. 여러 long-running async service 간 정보 흐름.

**관계**:
- minyoung-mah Orchestrator 는 v3 의 각 agent **안에서** 사용 (예: Scout 가 LLM 호출 시 minyoung-mah Orchestrator 활용).
- v3 Coordinator 는 **그 agents 위의 layer**. minyoung-mah 의 `Role` 개념 빌려서 v3 의 service-level role 정의.

**별도 layer 인 이유**:
- minyoung-mah Orchestrator 는 in-process / synchronous tool loop
- v3 service 는 stream-based / async / cross-process
- 둘은 동일 추상화로 표현 불가

minyoung-mah 에서 빌릴 개념:
- Role 추상화 (agent 의 책임 + interface 명세)
- structured input/output schema
- observability hook
- HITL (human-in-the-loop) pattern — 사용자 review 가 필요한 결정에 사용 가능

---

## 7. Observability

Coordinator 의 모든 의사결정 + 이벤트가 구조화 로그로 남는다:

- `decision_log` 테이블 (PG): `decision_id`, `kind`, `context_snapshot_json`, `outcome` (OK/NOT_OK), `reason`, `ts`
- `event_log` 테이블 (PG): 모든 Event Bus 이벤트 archive
- Telegram / Slack alert: Policy 가 critical reject 시
- Grafana dashboard: 의사결정 ratio, reject 사유 분포, 정책 효과 측정

→ Phase 0 retrospective 분석의 단일 source of truth.

---

## 8. 점진 도입 plan

### Stage 1. State Hub + Event Bus (관찰만)
- Coordinator 가 모든 이벤트 수신 + decision_log 기록
- agents 는 기존 path 그대로, Coordinator 는 read-only
- 운영 데이터 누적 (1~2주)
- 동시에 Phase 0 #1 분석이 decision_log 위에서 진행

### Stage 2. Advisory Decisions
- 매수/매도 결정 시점에 Coordinator query 추가
- Coordinator 가 "이 결정 의심" 알림만, 차단 X
- alert 빈도 분석 → false-positive 줄이기

### Stage 3. Enforce Mode
- Policy 확정 후 Coordinator 가 실제 차단 권한
- 가장 먼저 enforce: B2 duplicate_today, A1 max_rejected, F6 macro re-check
- 점진 확대

### Stage 4. Supervisor
- 각 task heartbeat 감시, die 한 task 만 restart
- FIRST_COMPLETED 패턴 제거 (E2 해결)

---

## 9. Phase 0 use cases 의 큰 그림

세 과제 모두 Coordinator 의 decision_log + event_log 위에서 진행:

**#1 Scout conviction-outcome correlation**
→ decision_log 의 `sheet_published` 이벤트 + `exit_filled` 이벤트 join. r > 0.3 검증.
→ 상세 design: 본 문서 §10 (이어서)

**#2 손절 % 진단**
→ decision_log 의 `entry_filled` + 후속 `exit_filled` 결합. 각 strategy_tag 별 손절 → 반등 분포 분석.
→ 5-14 데이터 (손절 5건 중 4건 반등) 가 baseline. 6주 데이터 누적 후 정책 조정.

**#3 Macro gate calibration**
→ decision_log 의 `policy_changed` (macro 전환) 이벤트 + 그 시점 portfolio outcome.
→ 5-12 KOSPI -2.29% 에 open 유지 사례를 알고리즘에 표현.

---

## 10. Phase 0 #1 — Scout conviction-outcome correlation (구체 design)

### 10.1 분석 목적

Scout 가 발행하는 `conviction` (0.0~1.0) 값이 실제 outcome (체결 후 pnl_pct) 과 상관 있는가? 메모리 [[project-vision-llm-autonomy-dropped]] 의 가설: r > 0.3 이면 conviction 을 sizing 에 반영. r ≈ 0 이면 발행 빈도/필터에만 활용. r < 0 이면 Scout 정책 재검토.

5-14 audit 결과 추가 가설: 시간대 / strategy_tag / macro_gate 별로 conviction 의 신뢰도가 다를 가능성. multi-dimensional 분석 필요.

### 10.2 데이터 소스

**기존 PG 만으로 가능한 분석**:
- `screening_candidates.conviction` × `executions.metadata.exit_reason` join via `promoted_to_sheet_id`
- 한계: 발행 시점의 macro / risk_level / 같은 ticker 직전 거래 history 가 컨텍스트로 안 묶임. multi-dim 분석 어려움.

**Coordinator decision_log 도입 후**:
- `decision_log` 에 시트 발행 시점의 전체 컨텍스트 snapshot 저장
- `sheet_published` 이벤트의 `context_snapshot_json` 에 macro_gate, macro_size_mult, risk_level, hour_of_day, today_entries_count, recent_exits_same_ticker 포함
- `entry_filled` / `exit_filled` 이벤트도 같은 decision_id 로 연결
- → 단일 시계열 view 에서 multi-dim correlation 가능

**점진 도입과의 정합성**:
- Stage 1 (State Hub + Event Bus 관찰만) 부터 decision_log 시작
- Phase 0 #1 분석은 1-2주 데이터 누적 후 가능 — Coordinator 도입 자체와 동시 진행
- 그 사이 retrospective 는 기존 PG 데이터만으로 일부 진행 가능 (macro/risk 컨텍스트는 macro_runs / intraday_risk:level 에서 사후 lookup)

### 10.3 분석 dimensions

기본 correlation:
- **r1**: conviction × pnl_pct (전체)

다차원 분석 (Coordinator 컨텍스트 활용):
- **r2**: conviction × pnl_pct, strategy_tag 별 (4 tag)
- **r3**: conviction × pnl_pct, macro_gate (open / closed 전환 직후)
- **r4**: conviction × pnl_pct, risk_level (NORMAL / CAUTION / WARNING / ...)
- **r5**: conviction × pnl_pct, hour_of_day (시초 09:15 burst vs 12:30 vs 14:30)
- **r6**: conviction × pnl_pct, "같은 ticker 가 오늘 N번째 추천" (1st / 2nd / 3rd+)
  → B1 의 self-reinforcing 패턴 정량화

추가 metric:
- **conviction 분포 stability**: 같은 ticker 의 hour-by-hour conviction 변동성 (5-14 066970 의 0.41 ~ 1.00 변동 패턴)
- **conviction 시계열**: 시간대별 평균 conviction 분포

### 10.4 retrospective query 예시

```sql
-- 기본 correlation (Coordinator 도입 전 PG 기반)
WITH sheet_outcomes AS (
  SELECT
    sc.scout_run_id,
    sc.ticker,
    sc.conviction,
    sc.strategy_tag,
    sc.created_at AS published_at,
    ex_buy.executed_at AS bought_at,
    ex_buy.price AS buy_price,
    ex_sell.executed_at AS sold_at,
    ex_sell.price AS sell_price,
    ex_sell.metadata_json->>'exit_reason' AS exit_reason,
    (ex_sell.price - ex_buy.price) / ex_buy.price AS pnl_pct
  FROM screening_candidates sc
  LEFT JOIN executions ex_buy
    ON ex_buy.sheet_id = sc.promoted_to_sheet_id AND ex_buy.side = 'buy'
  LEFT JOIN executions ex_sell
    ON ex_sell.sheet_id = sc.promoted_to_sheet_id AND ex_sell.side = 'sell'
  WHERE sc.created_at >= NOW() - INTERVAL '6 weeks'
    AND ex_buy.executed_at IS NOT NULL  -- 매수 됨
    AND ex_sell.executed_at IS NOT NULL  -- 매도 됨 (완결 케이스)
)
SELECT
  corr(conviction::float, pnl_pct::float) AS r,
  count(*) AS n
FROM sheet_outcomes;

-- strategy_tag 별
SELECT strategy_tag, corr(conviction::float, pnl_pct::float) AS r, count(*) AS n
FROM sheet_outcomes
GROUP BY strategy_tag;
```

→ Coordinator decision_log 도입 후엔 `context_snapshot_json` 의 macro_gate / risk_level / hour_of_day 도 같은 query 에서 GROUP BY 가능.

### 10.5 결과 해석 분기

| 시나리오 | r1 | 해석 | 활용 path |
|---|---|---|---|
| A | > 0.3 | conviction 신호 유효 | sizing 에 반영. `final_pct = base_pct × f(conviction)`. f 의 형태는 추가 분석. |
| B | 0.1 ~ 0.3 | 약한 신호 | conviction 임계값 (예: 0.7 이상만 발행) 으로 필터. sizing 반영 안 함. |
| C | -0.1 ~ 0.1 | 신호 없음 | conviction 무시. 발행 빈도 자체 재검토. |
| D | < -0.1 | 역신호 | Scout prompt / context 재설계 필요. 현 발행은 noise. |

다차원 결과로 분기 (r2~r6 활용):
- r1 약한데 r2 일부 strategy_tag 에서 강함 → tag 별 차등 적용
- r3 가 macro_gate 따라 다름 → macro 컨텍스트별 가중치
- r5 시초 시간대만 강함 → 시간 필터
- r6 1st > 2nd > 3rd 강하게 감소 → B1 패턴 정량 확인 + 발행 빈도 제한 정책

### 10.6 결과 활용의 Coordinator 내 위치

분석 결과는 정책으로 변환되어 Coordinator 의 Policy Engine 에 부착:

```python
# Stage 3 (Enforce mode) 예시
class ConvictionSizingPolicy:
    """r1 > 0.3 시나리오 — conviction 을 sizing 에 반영."""
    def evaluate(self, ctx: DecisionContext) -> PolicyResult:
        # f(conviction) 의 구체 형태는 분석으로 결정
        # 예: linear scale 0.5~1.0 (낮은 conviction 도 최소 50% 사이즈)
        sizing_mult = 0.5 + 0.5 * ctx.conviction
        return PolicyResult(
            adjusted_final_pct=ctx.final_pct * sizing_mult,
            note=f"conviction_sizing applied: conviction={ctx.conviction} mult={sizing_mult}",
        )
```

### 10.7 정기 모니터링 (regression 방지)

분석 1회로 끝나지 않고 Coordinator 가 매주 retrospective 자동 실행:
- correlation r 재계산
- 이전 주 대비 변화 ≥ 0.1 시 alert
- 시계열 plot → Grafana 패널

→ Scout prompt 가 v0.5 → v0.6 으로 변경되거나 LLM 모델 교체 시 효과 즉시 측정.

### 10.8 작업 단위 (Phase 0 #1 implementation)

1. **분석 script (Coordinator 도입 전 가능)**: `scripts/analyze_conviction_outcome.py`
   - 위 SQL 실행 + Pearson r 계산 + plot
   - macro / risk 컨텍스트는 macro_runs / Redis history 에서 사후 lookup
   - 견적: 200~300 줄. 1 일 작업.
2. **Coordinator State Hub + Event Bus 골격** (Stage 1):
   - decision_log 테이블 + event_log 테이블 migration
   - sheet_published / entry_filled / exit_filled 이벤트 발행 hook 추가 (기존 path 침해 최소)
   - 견적: 600~800 줄. 1~2 주 작업.
3. **재분석 + multi-dim**: Coordinator 데이터 1~2주 누적 후 진행
4. **정책화** (Stage 3): r 결과에 따라 ConvictionSizingPolicy / ConvictionFilterPolicy 구현
5. **정기 모니터링**: 주간 cron + Grafana 패널

작업 1 은 Coordinator 와 독립적으로 즉시 시작 가능. 작업 2 는 Coordinator design 확정 후. 둘 다 진행되며 작업 3-5 는 데이터 누적 후.

### 10.9 Phase 0 #2 / #3 도 같은 패턴

**#2 손절 % 진단** — `exit_filled` 이벤트의 `reason="fixed_sl"` 케이스 + 그 후 24h 가격 변동 추적. 5-14 데이터 (5건 중 4건 반등) 가 baseline. strategy_tag 별 적정 % 산출.

**#3 Macro gate calibration** — `policy_changed` (macro 전환) 이벤트 + 그 시점 portfolio outcome. 5-12 KOSPI -2.29% 에 open 유지 사례를 알고리즘 (예: 보조 metric 추가) 으로 표현.

둘 다 Coordinator decision_log + event_log 위에서 진행. 작업 단위는 #1 과 유사.

---

## 11. Open questions

- Coordinator 가 별도 컨테이너인가, fast_loop 안의 task 인가? (별도 service 가 깔끔하지만 latency / 복잡도 증가)
- decision_log 의 retention 정책 (분석은 6주 + 장기는 cold storage?)
- Stage 1 도입 시 기존 stream 패턴 유지 — Coordinator 는 listener-only 추가
- minyoung-mah 의 어느 부분을 직접 import 할지 (Role / Schema / Observability hook 만? Orchestrator 자체는 X)
- 매도 path 의 어느 decision 까지 Coordinator 가 검증? (forced_liquidation, STOP-triggered exit 등은 우회 필요)
