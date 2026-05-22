# v3 Assessment — 종목 선정 도메인 (selection)

작성: 2026-05-22 / 에이전트: selection / v2-teardown 2단계
대상: v3 = `prime-jennie-runtime` (이 repo, main `e708586`) + MS-01 라이브 운영
입력 체크리스트: `.ai/analyses/2026-05-22-v2-teardown-selection.md` §4 (v3 비교 훅 13항)
원칙: 코드·데이터가 진실. 양방향 정직(개선도 회귀도). 패딩 금지. 읽기 전용.
베이스라인 주의: 검증된 track record 는 v1, v2-native 는 2주·미검증. "v3가 v2보다
성과가 낫다/못하다" 식 판정은 하지 않음 — 비교축은 (a)설계 차원 v2 강점 유지/상실
(b)v3 라이브 데이터로 본 실제 상태.

핵심 사실 (먼저): **v3는 v2의 선정 설계를 뒤집었다.** v2 = 결정론 quant 코어 + LLM 은
±15 보정자(edge). v3 = **LLM 이 매 run 마다 스크리닝 Python 코드를 통째로 생성**(core) +
결정론은 sheet 조립·게이트(edge). v2 의 `quant.py` 같은 고정 스코어러가 v3엔 존재하지
않는다 — 실측: `scout_runs` 138 run **전부 distinct code_hash** (선정 로직이 같은 적이
한 번도 없음).

조사 범위: `slow_loop/{pipeline,app}.py`, `slow_loop/scout/*`, `slow_loop/macro/*`,
`slow_loop/strategy/*`, `screening_executor/*`, `jobs/factor_analysis.py`, v3 postgres
라이브(`scout_runs` 138 / `screening_candidates` 1,489 / `position_sheets` 1,355 /
`macro_runs` 309 / `scout_outcomes_v1` view), Redis. 데이터 기간 2026-04-17~05-22.

---

## 1. 훅별 판정표

| # | 훅 | 판정 | 근거 (file:line / 데이터) |
|---|---|---|---|
| 1 | 결정론/LLM 분리 | **LOST** | v2 는 LLM 을 quant±15 clamp 로 물리적 격리. v3 엔 clamp 대상이 될 결정론 baseline 자체가 없음 — LLM 이 스크리닝 코드 전체 생성(`code_loop.py`, `scout/prompts.py`). conviction(0-1)도 LLM 생성 코드가 계산. 결정론은 sandbox 실행·validator·engine(edge)로 이동. |
| 2 | 스코어링 추적성 | **LOST** | v2 `quant.py` 490줄 단일 파일·가중치 1개 dict. v3 스코어러는 매 run LLM 이 새로 쓴 Python — `scout_runs` 138 run = **138 distinct code_hash**. 리뷰 가능한 고정 스코어러 부재. (완화: `code_text`·`context_snapshot_json` 영속 — 사후 감사 가능) |
| 3 | 안정성 메커니즘 (MA/히스테리시스) | **LOST** | v3 엔 MA smoothing·히스테리시스·run 간 carryover 전무. validator 는 conviction 내림차순 top-20 + dedup 만(`validators.py:62-65`). 매 run 독립. v2 의 entry62/exit55 분리 임계 등가물 없음. |
| 4 | 섹터 분산 | **LOST** | v3 선정 경로에 섹터 cap 없음 — 프롬프트 "섹터 분산 고려" 권고문뿐(`prompts.py:206`). 실측: 단일 run `sr_20260515_0830` 후보 9개 중 금융 5개(56%). v2 는 greedy hard cap (11섹터 분산, 최대 4). |
| 5 | 데이터 부재 견고성 | **KEPT** | feeder 전부 fail-open(빈 list/dict), `context_builder` feeder별 try/except(`context_builder.py:188-198`), sandbox 구조화 에러 반환(`executor.py`), macro/scout retry×3. v2 수준 유지. |
| 6 | LLM 호출량·비용 | **IMPROVED (구조)** | v2 ~880 호출/일(종목당). v3 ~7 scout + 7~50 macro/일(run당 1회 code-gen). scout 비용 ~$0.005/run(총 $7.06/138run). 단 shadow(Opus)가 macro·scout 매 run 2배 — §4 참조. |
| 7 | raw 점수·후보 보존 | **IMPROVED** | v3 가 더 풍부: `scout_runs`(code_text, hypothesis, context_snapshot_json — 백테스트 재현), `screening_candidates`(raw 후보 전수 + rejection_reason + promoted_to_sheet_id), shadow 후보 JSON. v2 는 점수만. |
| 8 | 후보→sheet 경로 | **LOST** | v2 = quant→LLM→MA→budget→greedy→scanner→buyer (단계 많아도 각 단순·결정론). v3 = macro LLM(+retry3+shadow)→post-proc→scout LLM code-gen(+검증루프3+shadow)→sandbox→validator→engine→publisher. `pipeline.py` 1,032줄. 복잡한 부분이 경로 앞단으로 이동. |
| 9 | universe 구성 | **KEPT** | v2·v3 모두 `stock_masters` market_cap 내림차순 top 200 (`feeders/real.py:RealUniverseFeeder`, `SCOUT_UNIVERSE_SIZE` default 200). v3 는 market 필터·min_price 없음 — 사실상 동일, 약간 넓음. |
| 10 | 가중치 캡 버그 | **N/A (소멸)** | v2 의 V2_WEIGHTS 합 110/100 캡 버그는 v3 에 고정 가중치 체계가 아예 없어 발생 불가. `ScoutOutput.factor_weights` 는 LLM 자기신고 메타데이터 — 게이팅 미사용. |
| 11 | macro gate 성격 | **NEW-DEFECT** | v2 = soft tune(RSI 보정·섹터 cap). v3 = hard kill: gate=="closed"→scout phase 통째 skip(`pipeline.py:504`), engine 전 sheet 거부(`engine.py:405`). control STOP 은 macro 이전 early-return(`pipeline.py:322-336`). 실측: 5-18~5-20 `scout_runs`=0, `macro_runs` 5-19/20 도 0(STOP). |
| 12 | 죽은 코드 / drift | **NEW-DEFECT** | `jobs/factor_analysis.py` — v2 테이블 `daily_quant_scores`(v3 미적재, 2026-04-17 이후 0행) 조회 → 곧 윈도우 만료로 영구 0샘플. Redis `factor:analysis:latest` 기록하나 **읽는 코드 0건**(grep). `app.py:17` docstring "feeder Stub" — stale(real 배선됨). v2 패턴(stale prompt·dead 필드) 반복. |
| 13 | 실증 피드백 루프 | **NEW (비강제·약함)** | v3 `scout_outcomes_v1` view(position_sheets⋈executions, 133 청산완료) → scout 프롬프트 `previous_outcomes`(손절율·평균PnL 텍스트, `context_builder.py:123-156`). 단 **informational only, enforcement 없음**(G2 결정론 차단 연기). `weekly_factor_analysis` 는 죽음(#12). 루프가 "LLM 에게 손절율 보여주기"에 그침. |

요약: KEPT 3 / IMPROVED 2 / LOST 4 / NEW-DEFECT 2 / NEW(약함) 1 / N-A 1.

---

## 2. v3가 잃은 것 (회귀)

### R1. 결정론 코어의 소멸 — 선정 로직이 매 시간 다른 프로그램
v2 의 핵심 강점(v2 §2-A/B)은 "결정론이 선정을 소유하고 LLM 은 ±15 edge 보정"이었다.
v3 는 이를 역전 — `slow_loop/scout/code_loop.py` 가 LLM(DeepSeek)에게 `screen()`
Python 함수를 통째로 생성시키고 sandbox 실행 결과를 candidates 로 쓴다. **실측: 138
run 전부 다른 code_hash** — 어떤 종목이 왜 뽑혔는지 설명하는 "스코어러"가 고정 artifact
로 존재하지 않는다. v2 는 `quant.py` 한 번 읽으면 끝났다. v3 는 매 run `scout_runs.code_text`
를 따로 까봐야 한다. 비결정성이 edge case 로 새는 표면적(v2 가 구조적으로 최소화한 것)이
이제 선정 코어 전체다. (MEMORY `feedback_prompt_control_limit` — "결정론은 별도 layer"
원칙에서 멀어짐. v3 의 결정론 layer 는 sheet 조립뿐.)

### R2. 워치리스트 안정화 장치 전멸
v2 는 MA smoothing(3일) + 히스테리시스(entry 62 / exit 55) 로 일중 7런에도 종목
회전을 ~5-8개로 억제(v2 §2-D, distinct 30-33/일). v3 엔 등가물이 0 — `validators.py`
는 conviction 내림차순 top-20 + dedup 만 한다. 매 run 독립이라 LLM 코드가 바뀌면 후보도
통째로 바뀐다. 실측: v3 `position_sheets` 활성일 80~118 sheet/일, distinct ticker
40~52 — run 간 연속성을 보장하는 메커니즘이 코드에 없다.

### R3. 섹터 분산 강제 상실
v2 의 percentile 기반 동적 budget + greedy cap(v2 §2-E)은 단일 섹터 쏠림을 알고리즘적으로
불가능하게 했다. v3 는 섹터 cap 이 선정 어디에도 없다 — 프롬프트가 LLM 에게 "섹터 분산
고려"를 권하는 게 전부(`prompts.py:206`, 비결정 권고). 실측: `sr_20260515_0830` 단일
run 후보 9개 중 금융 5개(56%). 일 단위 집계는 분산돼 보이나 그건 7런 합산 착시 —
run 단위로는 쏠림 무제한.

### R4. 최소 컨빅션 floor 부재
v2 는 hard_floor 40 + entry_threshold 62 로 점수 낮은 종목을 매수 경로에서 차단했다.
v3 selection 엔 conviction 하한이 없다 — validator 도 engine 도 conviction 으로 게이팅
안 함. **실측: conviction 0.000 후보 3건 중 2건이 position_sheet 로 발행됨**
(`screening_candidates` conviction<0.3 구간 대량 promoted). LLM 코드가 0.0 확신으로
표시한 종목도 실매매 시트가 된다.

### R5. 경로 단순성 회귀
v2 의 후보→매수 경로는 단계가 많아도 각 단계가 단순·결정론이었다. v3 `pipeline.py`
1,032줄 — macro LLM(retry3+shadow) → post-processing → scout code-gen 검증루프(최대
3회, sandbox 재실행) → shadow scout → validator → engine → publisher → DLQ 분기.
복잡도가 경로 앞단(LLM·비결정)으로 옮겨갔고, 각 단계가 v2 처럼 "읽으면 끝"이 아니다.

---

## 3. v3의 진짜 개선

(미화 아님 — 설계상 분명한 진전만)

### I1. 결정론 closed-condition auto-override
`macro/closed_conditions.py` — fx_shock·high_volatility·sector_contagion·geopolitical·
liquidity_crunch 5종을 코드가 LLM 과 독립 재검증하고, LLM 이 open 을 내도 트리거 있으면
closed 강제(`post_processor.py:64-83`). v2 의 macro 는 LLM 출력을 그대로 신뢰했다 —
v3 는 macro LLM 위에 결정론 안전망을 얹었다. (단 그 결과가 hard kill 인 건 §4 D1.)

### I2. 후보 provenance·재현성
`scout_runs` 가 `code_text` + `code_hash` + `context_snapshot_json`(universe_hash,
news_events, sector_momentum, macro 상태)을 저장 — 특정 run 을 통째 재현 가능. shadow
(Opus) 후보까지 metadata 에 보존해 primary↔shadow 비교 가능. v2 는 점수 숫자만 남겼다.
백테스트·회귀 분석 입력으로는 v3 가 명백히 우수.

### I3. Scout 코드 ↔ sandbox 검증 닫힌 루프
`code_loop.py` — LLM 이 numpy import 누락·잘못된 반환 타입 등을 내면 sandbox 실행 에러를
다음 시도 프롬프트에 그대로 주입해 재생성(최대 3회), 같은 code_hash 반복 시 즉시 break
(ProgressGuard). v2 의 LLM 호출은 1회·실패하면 그냥 fallback 이었다. v3 는 LLM 산출물의
실행 가능성을 구조적으로 검증한다 — code-generation 패턴에 맞는 합리적 장치.

### I4. import allowlist 샌드박스
`screening_executor/allowlist.py` + `executor.py` — LLM 생성 코드를 import 검사 →
compile → exec 격리. 네트워크·파일 I/O·eval 차단. LLM 이 코드를 쓰는 이상 필수 방어이고,
v3 는 이를 제대로 갖췄다.

### I5. LLM 호출량 절감
v2 종목당 1-pass = ~880 호출/일. v3 run 당 code-gen 1회 = ~7 scout 호출/일. 호출 수
2 자릿수 감소. (단 shadow 가 이를 부분 상쇄 — §4 D2.)

---

## 4. v3의 새 결함 (특히 통합 이음매)

### D1. macro hard STOP — 선정 측정 윈도우 자체를 소거
v3 macro gate=="closed" 는 scout phase 를 통째 skip(`pipeline.py:502-513`),
control STOP 은 macro 이전 early-return(`pipeline.py:322-336`). v2 의 soft macro(약세장
에도 watchlist 25종목 생성, 방어는 하류 cash_floor 에 위임)와 정반대. **실측 피해:
`scout_runs` 5-18=0, 5-19=0, 5-20=0, 5-21=1** — STOP 이 v0.8 프롬프트 측정 윈도우를
통째로 막아 scout 표본이 안 쌓임(MEMORY `project_thesis_gate_deferred_2026_05_22`
와 동일 사건). 선정이 "멈출 수 있는 구조"가 됐는데, 멈추면 *학습·측정도 함께 멈춘다* —
shadow 비교도 outcome 피드백도 데이터가 안 들어온다. 진화가 아니라 결함.

### D2. shadow 이중 호출 — run 당 LLM 4회
`pipeline.py:412, 609` — macro·scout 마다 primary + shadow(Opus) 를 `asyncio.gather`
병렬 호출. 회귀 검증 목적은 타당하나, app.py docstring(2026-04-22)이 "shadow 로 1~2주
회귀 데이터 후 종료 가능"이라 했는데 5주째 상시 가동 중. shadow 산출물은
`scout_runs.metadata`/`macro_runs` JSON 에만 쌓이고 — primary 결정을 바꾸지 않는다.
즉 매 run Opus 호출 2회가 "비교용 로그" 외 소비처가 없다.

### D3. weekly_factor_analysis — 죽은 v2 이식
`jobs/factor_analysis.py` 는 v2 `/jobs/weekly-factor-analysis` 를 그대로 포팅했는데
`daily_quant_scores`(v2 테이블, v3 미적재 — 2026-04-17 이후 0행)를 조회한다. 30일
윈도우라 2026-05-15 run 은 잔존 v2 데이터 3일치로 **n=16** IC 를 계산(`factor:analysis:
latest` Redis: momentum IC 0.73 / quality IC −0.80 — n=16 노이즈, MEMORY
`feedback_single_day_overfit` 위반). 그 후엔 윈도우 만료로 영구 0샘플. 게다가
`factor:analysis:latest` 를 **읽는 코드가 0건**(grep 전수). 죽은 테이블 → 무의미한 통계
→ 아무도 안 읽는 Redis 키. v2 teardown §1.9 가 지적한 "전신의 죽은 피드백 잔존" 패턴을
v3 가 그대로 답습.

### D4. LLM 변동성 흡수 레이어의 누적 — 이음매 부패
strategy `engine.py` 의 `_normalize_scout_exit_hint`/`_normalize_scout_entry_hint` 는
LLM 이 schema 와 다른 형식을 내는 걸 흡수하는 방어 코드 — 주석에 "v0.3 prompt 결함",
"2026-05-13 MEAN_REVERT_RSI 2건 사고", "trailing_stop→trailing_tp 별칭" 등 사고
이력이 누적. scout 프롬프트는 v0.1→v0.8 (8개정/약 1개월). LLM 이 비정형 출력을 계속
내고, 매번 프롬프트 패치 + engine 정규화 레이어가 덧붙는 구조 — LLM↔engine 이음매가
끝없는 보수 비용을 발생시킴. **실측: `screening_candidates` 1,489건 중 `engine_error`
118건(7.9%)** — LLM 후보 데이터가 engine 예외를 일으킨 비율.

### D5. thesis_aware_hold — 측정 없는 over-engineering
G6 ThesisSpec: 프롬프트에 catalog 8종 가이드 45줄(`prompts.py:38-82`), schema 필드,
영속 로직까지 들어갔고 scout LLM 은 thesis_spec 을 emit 중(실측: 5-17 이후 sheet 14건
중 12건 thesis_spec 보유). 그러나 이를 평가할 revaluator(Phase B)는 6월로 연기 —
**현재 thesis_spec 은 생성·저장되지만 아무것도 읽지 않는다.** design doc 14절·다단계
Phase 분리는 MEMORY `feedback_design_doc_simplicity` 가 경계한 비대화 패턴. 측정도
못 한 채(STOP 으로 윈도우 소거, D1) 5-29 로 또 연기.

### D6. 중복 시트 — 2026-05-15 이전 dedup 무력
`engine.py` 주석: `NullActiveSheetChecker` 가 default 라 같은 ticker 중복 시트 차단이
"dead code"였고 `PgActiveSheetChecker` 가 audit B2(2026-05-15)에서야 실배선. 실측:
2026-05-05 `position_sheets` 118건/distinct 42 — 같은 종목이 매 run(7회) 시트 재발행.
약 한 달간 중복 시트가 그대로 발행됐다(하류 fast_loop 가 부분 차단했을 뿐).

---

## 5. step3 보완 후보 (우선순위·예상 규모 — elaborate 설계 금지)

P1·P2 는 "v2 가 갖고 있었으나 v3 가 잃은" 결정론 게이트의 복원이라 우선.

1. **[P1] 섹터 집중 cap 을 selection 에 복원** — `validators.py` 또는 engine 앞단에
   섹터별 max-N 결정론 cap. v2 `sector_budget.py` 로직 차용 가능. ~80줄. (R3)
2. **[P1] 최소 conviction floor** — engine `build_sheet_with_reason` 에 conviction <
   임계 거부 1줄 + config. conviction 0.0 시트 발행 차단. ~10줄. (R4)
3. **[P2] macro STOP 과 "측정 정지"의 분리** — STOP 시에도 scout code-gen + sandbox
   까지는 돌려 candidates·shadow 를 기록하되 sheet 발행만 막기. 선정 학습 데이터가
   STOP 으로 끊기지 않게. `pipeline.py` 분기 1곳. ~30줄 + 판단 필요. (D1)
4. **[P2] weekly_factor_analysis 결정** — 죽은 채로 두지 말고 (a) v3 `screening_candidates`
   + `scout_outcomes_v1` 기준으로 재배선하거나 (b) job·파일 삭제. 현 상태가 최악
   (죽은 코드 + 오해 소지). 재배선 시 ~120줄, 삭제 시 ~0. (D3)
5. **[P3] shadow 가동 종료 판단** — 5주 누적된 shadow 비교 데이터로 "primary 와
   유의미 차이 있나" 1회 분석 후, 없으면 shadow 호출 제거(run 당 Opus 2회 절감).
   분석 ~반나절, 제거 ~20줄. (D2)
6. **[P3] thesis_spec — Phase B 착수 또는 emit 중단** — revaluator 없이 catalog
   8종을 계속 emit·저장하는 건 순 비용. 6월 Phase B 를 진짜 할지 결정하고, 미루면
   프롬프트에서 thesis 가이드 제거해 scout 부담 경감. (D5)
7. **[P3] run 간 연속성 최소장치** — v2 히스테리시스 완전 이식은 과하나, "직전 run
   candidates 와의 overlap" 을 scout 프롬프트에 노출하는 정도(informational)면 저비용.
   ~40줄. 단 enforcement 아니므로 효과 제한적 — 우선순위 낮음. (R2)

---

## 부록 — 못 본 것 / 한계

- **선정→수익 연결**: `outcomes` 테이블 0행, `executions` 278건(2026-05-06~18, 2주).
  v3 선정 품질의 수익 검증은 표본·기간 부족 — execution/orchestration 에이전트 영역.
  본 평가는 "선정 메커니즘의 설계·구조"까지.
- **scout LLM 코드 품질의 정성 평가**: 138개 distinct screening_code 를 개별 정독하진
  않음. code_hash 전수 distinct·engine_error 7.9% 까지가 정량 근거.
- **shadow 비교 결과**: macro/scout shadow 가 primary 와 얼마나 일치하는지는
  `macro_runs`/`scout_runs` JSON 을 풀어야 하며 본 세션 범위 밖 — D5 P3 분석 항목으로 남김.
- v3 라이브 데이터는 2026-04-17 시작 — v3 selection 의 초기 1주(stub feeder 가능성)는
  실데이터로 구분 안 함.
