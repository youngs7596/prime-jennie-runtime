# `slow_loop/` — 느린 루프 (LLM 파이프라인)

Track B 소유. minyoung-mah `StaticPipeline`으로 오케스트레이션.

## 파이프라인 흐름 (실제 실행 순서)

```
MacroContextBuilder → Macro Role (LLM, REASONING) → raw MacroGateOutput
  → run_post_processing (결정론: auto_override, discretize, 이벤트)
  → MacroStateStore.set("macro:current_state")
  → if gate=="closed": early return (Scout skip)

ScoutContextBuilder → Scout Role (LLM, STRONG) → ScoutOutput
  → ScreeningToolAdapterStub.invoke (Track D 연결 자리)
  → validate_candidates (S06 truncate, S07 dedupe, S08 hallucination)
  → for each cand: StrategyEngine.build_sheet (결정론)
  → PositionSheetPublisher.publish (→ Redis v3:position_sheets)
```

LLM 호출은 Macro, Scout 각 한 번씩만 (fast path: `output_schema` + `max_iterations=1` + `tool_allowlist=[]`). 나머지는 전부 결정론.

## 서브모듈

| 디렉토리 | 역할 | minyoung-mah 프로토콜 |
|---|---|---|
| `scout/` | 스크리닝 Python 코드 생성 | `SubAgentRole` fast path |
| `macro/` | 바이너리 게이트 + size_multiplier | `SubAgentRole` fast path |
| `strategy/` | 결정론적 룰엔진 → PositionSheet 발행 | 프로토콜 미사용 (LLM 금지) |

### `scout/`
- `schemas.py` — ScoutOutput, ScreeningCandidate, EntryHint, ExitHint, NewsEventEntry, ScoutContext
- `role.py` — ScoutRole (SubAgentRole 구현, tier="strong")
- `prompts.py` — SCOUT_SYSTEM_PROMPT + build_user_prompt
- `code_hasher.py` — sha256 해시 (provenance.scout_code_hash, S13 재현성)
- `validators.py` — S06/S07/S08 검증
- `screening_stub.py` — Track D 컨테이너 자리 표시자 (고정 candidates 반환)
- `context_builder.py` — 입력 feeder 합성 → ScoutContext
- `feeders/` — UniverseFeeder / NewsEventFeeder / SectorMomentumFeeder / MarketSummaryFeeder protocol + stub

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

### `thesis/` (Phase 1 도입 예정, 5-22~)

`thesis_aware_hold` 가드 — 보유 sheet 의 `ProvenanceSection.thesis_spec` (catalog 5종 condition) 을 정기 평가, `critical_conditions` 깨지면 `forced_liquidation:thesis` Redis SET 적재 → fast_loop 즉시 매도.

- Phase A (DONE 2026-05-17 commit `498264d`): `ThesisSpec` schema + Scout prompt v0.8 catalog 가이드 + ProvenanceSection 영속만. revaluator 미포함.
- Phase 1 (5-22 ~ 5-29 advisory): `thesis/evaluators.py` (catalog 함수) + `thesis/revaluator.py` (1시간 cron). log + telegram, 매도 X.
- Phase 2 (5-29 ~ enforce): `forced_liquidation:thesis` 적재 + fast_loop `tick_loop._evaluate_forced_liquidation` 두 SET 분리 (`:user` / `:thesis`).

결정: [`.ai/designs/2026-05-17-g-series-simplification.md`](../../.ai/designs/2026-05-17-g-series-simplification.md)

## 핵심 규칙

- `scout/`는 코드를 생성하지 종목을 추천하지 않는다 (SCOUT_CODE_GENERATION.md)
- `macro/`의 reasoning / top_risks / confidence는 로깅 전용. **실행 로직 참조 절대 금지** (MACRO_GATE_SPEC.md §1.2, §7)
- `strategy/`는 LLM 호출 금지. 모든 결정은 결정론 코드
- 공유 스펙(`position_sheet/schema.py`, `strategy_policy.yaml`) 변경 시 stop-the-world

## 위임 경계

| 영역 | 현 상태 | 완성 시 교체 |
|---|---|---|
| Scout 코드 실제 실행 | `ScreeningToolAdapterStub` | Track D `ScreeningToolAdapter` (Docker 격리) |
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
- `pj.scout.code_generated` — Scout LLM 성공
- `pj.scout.hallucination_suspected` — 30% warn / 50% fail
- `pj.scout.no_candidates` — fallback 발동
- `pj.scout.output_missing` / `pj.scout.retry` / `pj.scout.failed`

Strategy:
- `pj.strategy.sheet_published` — 정상 발행
- `pj.strategy.sheet_rejected` — engine이 None 반환 (macro closed, min_pct, 중복, deprecated)
- `pj.strategy.sheet_error` — engine 예외

Slow Loop:
- `pj.slow_loop.skipped_macro_closed` — Macro closed로 Scout 생략

## 로컬 워크어라운드 (minyoung-mah 기능 추가 대기)

- Role retry (3회) — pipeline.py의 `_run_pipeline_with_retry`. 에러 컨텍스트를 다음 프롬프트에 주입하는 건 Phase 2로 연기.
- Scout/Macro 간 조건부 분기 — `run_slow_loop()` 래퍼에서 직접 `if gate == "closed": return`
- 결정론 후처리 단계 — pipeline이 아닌 래퍼 함수에서 호출 (ExecuteToolsStep 사용 부자연)

2회 이상 반복되면 minyoung-mah PR 후보 (Phase 0 design §3.3).
