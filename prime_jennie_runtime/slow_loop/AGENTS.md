# `slow_loop/` — 느린 루프 (Macro LLM Gate + 결정론 Scout)

Track B 소유. Macro Gate 는 minyoung-mah `StaticPipeline` 으로 오케스트레이션,
Scout 선정은 결정론 함수(`run_deterministic_scout`) 호출.

## 파이프라인 흐름 (실제 실행 순서)

```
MacroContextBuilder → Macro Role (LLM, REASONING) → raw MacroGateOutput
  → run_post_processing (결정론: auto_override, discretize, 이벤트)
  → MacroStateStore.set("macro:current_state")
  → if gate=="closed": early return (Scout skip)

ScoutContextBuilder → run_deterministic_scout (결정론 quant 코어, LLM 0회)
  → enrich_universe → 7팩터 quant 스코어 → MA 평활 → 히스테리시스 선정
  → persist_scout_run / persist_screening_candidates
  → validate_candidates (S06 truncate, S07 dedupe, S08 hallucination)
  → for each cand: StrategyEngine.build_sheet (결정론)
  → PositionSheetPublisher.publish (→ Redis v3:position_sheets)
```

LLM 호출은 **Macro Gate 한 번뿐** (fast path: `output_schema` + `max_iterations=1`
+ `tool_allowlist=[]`). Scout 선정·Strategy 발행은 전부 결정론. 매 실행 LLM codegen
은 2026-05-22 Phase 1 에서 폐기됐다 — `.ai/decisions/2026-05-22-selection-architecture-decision.md`.

## 서브모듈

| 디렉토리 | 역할 | minyoung-mah 프로토콜 |
|---|---|---|
| `scout/` | 결정론 quant 선정 (universe → 7팩터 채점 → 선정) | 미사용 (LLM 호출 0회) |
| `macro/` | 바이너리 게이트 + size_multiplier | `SubAgentRole` fast path |
| `strategy/` | 결정론적 룰엔진 → PositionSheet 발행 | 프로토콜 미사용 (LLM 금지) |

### `scout/` — 결정론 quant 선정 (2026-05-22 Phase 1, v2 코어 포팅)
- `deterministic_scout.py` — 오케스트레이터. universe → 후보 `list[ScreeningCandidate]` (`run_deterministic_scout`)
- `quant.py` — v2 7팩터 스코어러 포팅 (momentum / quality / value / technical / news / supply_demand / sector_momentum)
- `enrichment.py` — universe 종목별 일봉·재무·컨센서스·스냅샷 적재 → `EnrichedCandidate`
- `selection.py` — MA 평활 + 히스테리시스 선정
- `schemas.py` — ScoutOutput, ScreeningCandidate, EntryHint, ExitHint, NewsEventEntry, ScoutContext
- `context_builder.py` — 입력 feeder 합성 → ScoutContext
- `validators.py` — S06/S07/S08 검증 (universe 밖 ticker = hallucination)
- `feeders/` — UniverseFeeder / NewsEventFeeder / SectorMomentumFeeder / MarketSummaryFeeder protocol + stub

은퇴 — LLM codegen 경로 (2026-05-22 폐기, 라이브 미사용 / 삭제 예정):
- `role.py`, `prompts.py`, `code_loop.py`, `code_hasher.py`, `screening_stub.py`

### `macro/`
- `schemas.py` — MacroGateOutput, RiskItem, MarketSnapshot, IndexPoint, SectorDrop, MacroContext, RecentMacroRun
- `role.py` — MacroGateRole (tier="reasoning")
- `prompts.py` — MACRO_SYSTEM_PROMPT + build_user_prompt
- `discretize.py` — half-open 이산화 (MG15~MG22)
- `closed_conditions.py` — 결정론 closed 트리거 (MG02)
- `continuity.py` — abrupt transition 체크 (MG07)
- `post_processor.py` — LLM raw → 최종 output + 이벤트 발행
- `state_store.py` — Redis `macro:current_state` + 24h stale (MG08)
- `context_builder.py` — feeder 합성 → MacroContext
- `feeders/` — WsjDigestFeeder / MarketSnapshotFeeder / KorMacroNewsFeeder protocol + stub

### `strategy/`
- `strategy_policy.yaml` — strategy_tag별 base_pct / max_notional_krw / default exit rules (영석 수동 관리)
- `policy.py` — YAML 로더 (StrategyPolicy, StrategyEntry)
- `sheet_id.py` — `ps_YYYYMMDD_TTTTTT_HHHH` 생성기 (재현 가능)
- `risk_throttle.py` — RiskThrottleSnapshot Protocol + NoOpRiskThrottle(1.0). Track C가 실제 throttle 주입
- `engine.py` — StrategyEngine.build_sheet (결정론). macro closed / MIN_PCT / 중복 / deprecated tag 거부
- `publisher.py` — PositionSheetPublisher (`v3:position_sheets` + DLQ)

### 루트
- `pipeline.py` — `SlowLoopComponents` 묶음 + `run_slow_loop()` 래퍼
- `persistence.py` — macro_runs / scout_runs / screening_candidates 영속

### `thesis/` (Phase 1 — 재검토 중)

> ⚠️ 2026-05-22 Phase 1 으로 LLM Scout 가 폐기되며 결정론 scout 는 `thesis_spec` 을
> emit 하지 않는다 (`deterministic_scout._to_screening_candidate` → `thesis_spec=None`).
> 아래 로드맵은 LLM Scout 의 thesis_spec 반환을 전제했으므로 thesis gate 재설계 대기.
> `.ai/sessions/session-2026-05-22-0001.md` §4.5.

`thesis_aware_hold` 가드 — 보유 sheet 의 `ProvenanceSection.thesis_spec` (catalog 5종 condition) 을 정기 평가, `critical_conditions` 깨지면 `forced_liquidation:thesis` Redis SET 적재 → fast_loop 즉시 매도.

- Phase A (DONE 2026-05-17 commit `498264d`): `ThesisSpec` schema + Scout prompt v0.8 catalog 가이드 + ProvenanceSection 영속만. revaluator 미포함.
- Phase 1 (5-22 ~ 5-29 advisory): `thesis/evaluators.py` (catalog 함수) + `thesis/revaluator.py` (1시간 cron). log + telegram, 매도 X.
- Phase 2 (5-29 ~ enforce): `forced_liquidation:thesis` 적재 + fast_loop `tick_loop._evaluate_forced_liquidation` 두 SET 분리 (`:user` / `:thesis`).

결정: [`.ai/designs/2026-05-17-g-series-simplification.md`](../../.ai/designs/2026-05-17-g-series-simplification.md)

## 핵심 규칙

- `scout/`는 결정론 quant 스코어러로 종목을 선정한다 — LLM codegen 폐기 (2026-05-22). 선정 경로 LLM 호출 0회
- `macro/`의 reasoning / top_risks / confidence는 로깅 전용. **실행 로직 참조 절대 금지** (MACRO_GATE_SPEC.md §1.2, §7)
- `strategy/`는 LLM 호출 금지. 모든 결정은 결정론 코드
- 공유 스펙(`position_sheet/schema.py`, `strategy_policy.yaml`) 변경 시 stop-the-world

## 위임 경계

| 영역 | 현 상태 | 완성 시 교체 |
|---|---|---|
| news_events | `StubNewsEventFeeder` (고정값) | Track E `news_pipeline_kor` |
| WSJ digest | `StubWsjDigestFeeder` | Track E digest pipeline |
| market data | `StubMarketSnapshotFeeder` / `StubMarketSummaryFeeder` | Track E KRX/v2 legacy_* |
| RiskThrottle | `NoOpRiskThrottle(1.0)` | Track C 실제 throttle |

## Observer 이벤트 (pj.* 네임스페이스)

Macro:
- `pj.macro.gate_closed` — closed 확정 (LLM이든 auto_override든)
- `pj.macro.auto_override` — LLM open + 결정론 트리거 있어 closed 강제
- `pj.macro.inconsistent_open_zero` — open + 0.0 모순 → 0.25 강제
- `pj.macro.abrupt_transition` — delta >= 0.5 급격 전환
- `pj.macro.stale_detected` — 24h 오래된 상태
- `pj.macro.output_missing` / `pj.macro_gate.retry` / `pj.macro_gate.failed`

Scout:
- `pj.scout.code_generated` — 결정론 scout 완료 (이벤트명은 LLM codegen 시절 잔존)
- `pj.scout.fallback_use_previous_run` / `_empty` / `_unavailable` — 직전 run 후보 재사용 fallback
- `pj.scout.hallucination_suspected` — universe 밖 ticker 30% warn / 50% fail
- `pj.scout.no_candidates` — 후보 0

Strategy:
- `pj.strategy.sheet_published` — 정상 발행
- `pj.strategy.sheet_persisted_no_emit` — STOP 중 — 시트 영속·분석은 하되 emit 만 차단
- `pj.strategy.sheet_rejected` — engine이 None 반환 (macro closed, min_pct, 중복, deprecated)
- `pj.strategy.sheet_error` — engine 예외

Slow Loop:
- `pj.slow_loop.skipped_macro_closed` — Macro closed로 Scout 생략
- `pj.slow_loop.publish_blocked_control` — control STOP 등으로 발행 차단
- `pj.slow_loop.dlq_sent` — pipeline 단계 실패분 DLQ 송부

## 로컬 워크어라운드 (minyoung-mah 기능 추가 대기)

- Macro Role retry (3회) — pipeline.py의 `_run_macro_with_retry` / `_run_pipeline_with_retry`. 에러 컨텍스트를 다음 프롬프트에 주입. (Scout 은 결정론 — retry 없음)
- Scout/Macro 간 조건부 분기 — `run_slow_loop()` 래퍼에서 직접 `if gate == "closed": return`
- 결정론 후처리 단계 — pipeline이 아닌 래퍼 함수에서 호출 (ExecuteToolsStep 사용 부자연)

2회 이상 반복되면 minyoung-mah PR 후보 (Phase 0 design §3.3).
