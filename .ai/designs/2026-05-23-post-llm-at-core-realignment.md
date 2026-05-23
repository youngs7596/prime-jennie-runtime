# 결정론 코어 전환 후 큰 그림 재정렬 (2026-05-23)

**작성**: 2026-05-23 (토)
**전제 commit**: a65db2e (5-22 결정론 선정 코어 복원, LLM-at-core 폐기)
**대체 대상**: 5-14 ~ 5-17 사이에 작성된 design doc 6 편의 상태 일괄 정리

## 1. 왜 이 글이 필요했는가

지난 일주일 동안 가장 큰 변화가 5-22 의 핵심 갈아엎기였다. v3 가 매 scout 실행마다 LLM 으로 screening 코드를 통째 생성하던 LLM-at-core 구조를 폐기하고, v2 의 결정론 quant.py 7 팩터 스코어러 + MA 평활 + 히스테리시스를 그대로 포팅했다. 선정 경로의 LLM 호출은 0 회가 됐다.

그런데 5-14 부터 5-17 까지 일주일 동안 작성한 design doc 들은 모두 LLM-at-core 전제 위에 그려졌다. Scout 가 자연어 thesis 를 발행하고 critical_conditions 를 카탈로그에서 선택하고 conviction 값을 LLM 이 정한다는 게 깔린 채로 쓰여 있다. 5-22 전환 후엔 그 전제 일부가 깨졌다.

이 글은 옛 doc 6 편의 항목을 한 번에 분류한다. 그러나 그보다 먼저 짚는 게 더 중요하다. 그 doc 들이 **왜 만들어졌는지** 의 큰 그림을 회복해야 한다. 큰 그림이 살아있어야 핵심 갈아엎기 이후의 다음 행동이 같은 방향으로 정렬된다.

## 2. 모든 design doc 의 단일 출발점 — 5-15 한 번의 사고

5-15 오전 09:00 ~ 11:13 사이에 손절 10 건이 한꺼번에 났다. 합 -3.35 백만 원. 분석 결과 일회성 사건이 아니라 네 가지 구조적 결함이 동시에 드러난 것이었다 (5-15 scout-overextension doc §2).

- 입력 데이터가 어제 일봉 60 일까지였다. 시초 갭다운을 모르는 상태로 추천이 나간다.
- 평가 방향이 모멘텀 일변도였다. 이미 너무 오른 종목을 회피하는 가이드가 없었다.
- Scout 가 자기 직전 run 의 결과를 모르는 채로 다음 run 을 돈다. 09:30 cron 이 어제 손절된 종목을 또 추천했고, 11:10 cron 이 같은 패턴을 한 번 더 반복했다.
- 같은 종목을 24 시간 안에 반복 추천하는 것을 차단하는 가드가 없었다.

이 네 결함을 사용자가 5-17 simplification doc 머리에 두 문장으로 압축했다. "slow_loop 이 정보 결손으로 같은 손실 패턴 반복, 그리고 hold/exit 결정이 시장 상태 변화 무관." 이 두 문장이 모든 design doc 의 큰 그림이다.

## 3. 두 결손이 만든 두 줄기

5-14 부터 5-17 사이의 design doc 들은 표면적으로 G1, G2, G3, G4, G5, G6, Coordinator, cooldown 등 일곱 여덟 가지처럼 보이지만, 큰 그림에선 두 줄기다.

### 3.1 첫 번째 줄기 — 정보 흐름의 외톨이 깨기

5-14 v3 audit 가 이 줄기의 출발점이다. Scout 가 자기 한 시간 전 추천을 모르고, Strategy Engine 이 같은 거래일 이미 발행한 시트를 모르고, fast_loop consumer 가 손절 history 를 모르고, tick_loop 이 다른 task 의 heartbeat 를 모른다. 정보가 한쪽으로만 흐르고 사후 피드백 채널이 없는 상태였다.

5-14 Coordinator design 의 State Hub (현재 상태 단일 view), Decision Authority (의사결정 승인 layer), Event Bus (모든 결정·체결·정책 변경 이벤트) 세 부분이 이 결손을 푸는 골격이다. 5-15 cooldown-and-duplicate-guard 와 5-15 scout-overextension 의 G1 outcome feedback, G5 today_exit_cooldown 은 그 골격 안의 즉시 적용 항목들이다.

### 3.2 두 번째 줄기 — 진입 가설 (thesis) 의 사후 추적

진입할 때 깔았던 가설이 다음 날 무너졌는지를 보고 보유를 끊자는 줄기다. 5-17 simplification doc 표에서 "B 진짜 메인" 으로 표시된 항목이고, 5-17 G6 thesis-aware-exit doc 이 이 줄기의 상세 설계였다.

옛 G2 overextension validator 와 G3/G4 시초 timing 트랙은 이 두 줄기와 직교하는 별개 갈래였다. G2 는 5-17 Pre-flight 분석에서 임계값이 손익을 구분하지 못한다는 결과로 이미 폐기됐고, G3/G4 는 backlog 로 남았다.

## 4. 모든 doc 의 공통 원칙

5-15 scout-overextension doc §3 에 한 줄로 박혀 있다.

> "모든 enforcement 는 결정론 코드 layer. Scout prompt 변경은 정보 노출만, 차단은 후행 layer."

이게 사용자가 정립한 글로벌 메모리 `feedback_prompt_control_limit` 의 핵심이다. minyoung-mah 학습이 같은 결이다. LLM 의 비결정성을 prompt 로 막으려 하면 반드시 edge case 로 새어 나간다. 결정론 enforcement 는 별도 layer 에 둔다.

## 5. 5-22 결정론 코어 전환의 의미

이 원칙 위에서 보면 5-22 의 결정론 코어 복원은 옛 design doc 들의 정신을 배반한 결정이 아니다. 오히려 그 원칙을 코어까지 밀어 넣은 가장 큰 적용이다. Scout 자체를 비결정성에서 결정성으로 옮긴 것이다. 외부 검토 (Gemini, Claude Web) 가 독립적으로 같은 결론에 수렴한 것도 같은 줄기다.

남는 질문은 옛 doc 들의 항목이 결정론 코어 위에서 그대로 의미가 있는지, 출처가 바뀌어 재설계해야 하는지, 아예 사라졌는지를 항목별로 가르는 것이다.

## 6. 결정론 코어가 실제로 무엇을 발행하는지

분류에 앞서 코드와 데이터로 사실 확인을 했다.

**결정론 코어 (deterministic_scout.py) 가 발행하는 필드**

- `conviction` — 발행한다. 값은 MA 평활된 ma_score / 100, 0 ~ 1 클램프. 5-22 이후 screening_candidates 29 건의 평균 0.612, 분포 0.400 ~ 0.685.
- `strategy_tag` — 발행한다. v2 quant.py 에는 없던 어댑터 함수 `_assign_strategy_tag` 가 단순 결정론 룰로 배정한다 (SECTOR_MOMENTUM, EARNINGS_DRIFT 등).
- 7 팩터 서브점수 (momentum, quality, value, technical, news, supply_demand, sector_momentum) — score_metadata 에 들어간다.
- `thesis_spec` — **None 으로 발행한다.** 결정론 코어는 자연어 가설을 만들지 않는다.

**스키마 쪽**

- `screening_candidates` 에 conviction 컬럼 살아있음. 채워지고 있음.
- `position_sheets` 에 thesis 관련 컬럼 자체가 없음. strategy_tag 만 있음.
- `daily_quant_scores` 가 5-22 부터 적재 시작. 246 행, 123 종목 × 2 run. 결정론 베이스라인이 누적되기 시작했다.

**Coordinator 인프라**

- coordinator-listener 컨테이너 4 시간째 가동.
- event_log 5-22 이후 19,508 건의 entry_decided + 58 건의 entry_rejected + 15 건의 sheet_published + 3 건의 entry_filled. 58 entry_rejected 는 전부 KIS API reject 이고 advisory 정책 발화가 아니다. 5-15 cooldown 가드가 fast_loop L1 에서 차단하므로 L2 advisory 발화가 자연 발생하지 않는 게 정상이다.
- decision_log 는 2 건뿐이라 활용 빈도 낮음.

## 7. 메모리 정정 한 가지

직전 분석 글들이 "결정론 quant 가 현재 conviction 을 발행하지 않아 Phase 0 #1 (conviction-outcome 상관 분석) 을 5-29 이후로 보류" 라고 적어 두었다. 이 진술은 부정확하다. 결정론 코어가 conviction 을 발행하고 있고, 5-22 부터 데이터가 쌓이고 있다. 메모리의 `project_selection_architecture_decision` 과 `project_thesis_gate_deferred_2026_05_22` 를 다시 보고 정정해야 한다.

## 8. 두 줄기가 결정론 코어 후에 어떻게 재정렬되는가

### 8.1 첫 번째 줄기 — 외톨이 깨기

이 줄기는 코어 전환과 독립이다. 결정론 Scout 도 자기 직전 run 의 outcome 을 모르고, 결정론 Scout 가 발행한 시트도 다음 시점의 macro 변동을 모른다. 외톨이 문제 자체는 그대로 살아있다.

Coordinator 인프라가 실제로 가동 중인 게 확인됐다 (event_log 거의 2 만 건). 5-15 cooldown 가드는 L1 enforcement 가 fast_loop 에 들어가 있고, L2 advisory 는 발화 케이스가 자연 발생하지 않아 0 건이다. 같은 거래일 duplicate 가드도 마찬가지다. 이 줄기의 골격은 살아 있다.

Phase 0 #1 conviction-outcome 상관 분석은 결정론 코어 conviction 위에서 즉시 가능하다. 보류가 아니라 데이터가 쌓이는 동안 분석 script 만 준비하면 된다. 5-14 Coordinator doc §10 의 SQL 예시가 그대로 적용 가능하다 (screening_candidates.conviction × executions 의 pnl_pct).

### 8.2 두 번째 줄기 — thesis 줄기는 출처가 끊겼다

이 줄기는 가장 크게 바뀐다. 결정론 코어가 thesis_spec=None 으로 발행하고 position_sheets 스키마에도 thesis 컬럼이 없다. 옛 G6 가 깔았던 "Scout LLM 이 발행한 thesis 의 critical_conditions 가 다음 날 살아있는지" 라는 평가 방식은 출처가 통째로 끊겼다.

그러나 동기는 그대로 살아있다. hold/exit 결정이 시장 변화에 무관하다는 문제는 결정론 코어로 갈아도 그대로다. 7 팩터 점수가 어제와 다르게 움직이는 것을 thesis 변화로 본다든지, daily_quant_scores 의 시계열 변동을 hold 신호로 쓴다든지 하는 새 경로가 가능하다. 5-23 Temporal Context PoC 의 Fact layer 가 사실은 이 새 경로와 같은 자리에 있다.

세 갈래로 좁힐 수 있다.

- (A) 7 팩터 점수의 시계열 변동을 thesis 대체 신호로 쓴다. 자연어 thesis 없이 결정론적 점수 변화로 hold 판단.
- (B) thesis 만 따로 발행하는 별도 LLM 채널을 둔다. enforcement 와 분리된 흐름으로, 5-12 폐기된 LLM 자율성 비전과의 경계를 명확히 다시 그어야 한다.
- (C) thesis 추적을 5-23 Temporal Context PoC 의 Fact layer 에 흡수한다. 결정론 추출은 누적 가능 (Fact), LLM 해석은 휘발 (Interpretation) 이라는 분리에 맞춰 thesis 신호도 사실 층으로 옮긴다.

(C) 가 가장 자연스럽다. 5-22 결정론 코어가 Fact layer 의 일부를 이미 만들어 준 상태이기 때문이다. PoC 와 thesis 줄기가 같은 자리로 합쳐진다.

## 9. 옛 design doc 6 편의 상태 일괄 결정

| doc | 상태 | 사유 |
|---|---|---|
| 2026-05-14-agent-coordinator.md | active | State Hub / Decision Authority / Event Bus 골격이 그대로 살아있고 인프라가 가동 중. Phase 0 use cases 의 #1 conviction-outcome 은 결정론 코어 위에서 즉시 가능. #2·#3 도 그대로 유효. 단 §10 의 conviction 출처 설명은 결정론 코어 기준으로 한 줄 갱신 필요. |
| 2026-05-15-cooldown-and-duplicate-guard.md | active | L1 enforcement 가 fast_loop 에 도입되어 운영 중. L2 advisory 는 event_log 적재 정상. 코어 전환과 독립이라 그대로 유효. |
| 2026-05-15-scout-overextension-guards.md | 부분 active / 부분 재설계 | G1 outcome feedback 의 "Scout context 노출" 부분은 결정론 Scout 가 context 를 받지 않으므로 무의미. 다만 결정론 코어의 입력 데이터에 outcome 반영 여부는 별개 질문으로 재설계 필요. G2 (overextension validator) 는 archive. G3/G4 (시초 timing) 은 backlog 유지. G5 (today_exit_cooldown) 은 도입 완료. |
| 2026-05-17-g-series-simplification.md | 부분 archive / 부분 재설계 | outcome_feedback (구 G1) 의 "Scout context 노출" 은 결정론 코어로 의미 변질. same_day_cooldown (구 G5) 은 도입 완료. thesis_aware_hold (구 G6) 는 출처 끊김으로 재설계 필요 — §8.2 의 세 갈래 (A/B/C) 중 선택. |
| 2026-05-17-g2-overextension-validator.md | archive | 5-17 Pre-flight 부정 결과로 이미 deprecated. 본 글로 archive 확정. |
| 2026-05-17-g6-thesis-aware-exit.md | archive | thesis 출처가 결정론 코어로 끊김. §8.2 의 새 갈래 결정 후 별도 design doc 으로 시작. 본문은 학습 산출물로 보관. |

각 doc 의 머리 부분에 본 글 (2026-05-23-post-llm-at-core-realignment.md) 을 status header 로 cross-ref 한다. 본문은 archive 의도라도 학습 산출물로 보관한다.

## 10. 다음 행동

큰 그림이 정리된 직후의 우선순위는 두 가지다.

첫째, Phase 0 #1 conviction-outcome 상관 분석을 결정론 코어 위에서 시작한다. screening_candidates 의 5-22 이후 conviction 과 executions 의 pnl_pct 를 join 하는 분석 script 를 준비한다. 데이터는 1 주 ~ 2 주 누적 후 의미 있는 표본이 된다. 그 사이 script 만 작성해 두면 5-29 이후에 바로 돌릴 수 있다.

둘째, thesis 줄기는 (C) **5-23 Temporal Context PoC 의 Fact layer 에 흡수** 로 확정 (2026-05-24 결정). 별도 design doc 을 새로 만들지 않고 PoC 안에서 다룬다. PoC 본격 구현은 5-29 이후이므로 thesis 줄기 시작도 같은 시점이다. PoC design (`2026-05-23-temporal-context-poc.md`) §5 에 두 설계 원칙을 추가했다 — 다음 §11 에 그 두 원칙을 함께 정리한다.

Phase 0 #2 (손절 진단 후속) 와 #3 (Macro 게이트 보정 후속) 는 큰 그림 재정렬과 독립이라 기존 계획대로 진행하면 된다.

## 11. thesis 추적을 PoC 에 흡수한 후의 두 설계 원칙

사용자가 (C) 결정 시점에 두 가지 의문을 제기했다. 하루만에 thesis 무너짐을 평가할 수 있는가, 그리고 가설은 무너졌지만 이익을 보고 있는 종목은 어떻게 처리하는가. PoC design 에 두 원칙으로 반영했다.

**원칙 1 — 다중 horizon 과 지속성을 함께 본다**

일별 점수 변동은 노이즈가 크다. 옛 G2 가 "가격 기반 지표가 outcome 예측력 없음" 으로 폐기된 학습이 같은 결이다. PoC 는 한 시점 값이 아니라 1 일·3 일·5 일 변화를 함께 보고, 변화의 지속성 (사흘 연속 음의 변화 같은 것) 을 신호로 쓴다. 임계값은 사전에 박아 두지 않고 30 일 표본 위에서 사후 검증한다. PoC 의 Kill criteria 중 stability 항목이 이 검증의 베이스가 된다.

**원칙 2 — thesis 신호는 exit 결정을 단독으로 내리지 않는다**

옛 G6 Phase 2 enforce 의 default 였던 "thesis invalidated → forced_liquidation" 은 이익을 보고 있는 포지션을 강제로 끊을 수 있는 위험이 있었다. PoC 안에서는 thesis 신호가 Coordinator 의 advisory event 로만 나가고, exit 결정은 fixed_sl·trailing_tp·breakeven·thesis_invalidated 네 신호를 종합하는 별도 결정론 layer 가 내린다. 이익 구간에선 trailing_tp 가 우선이고, thesis 무너짐은 trailing 임계값을 좁히는 방향으로만 작용한다. enforce 단계는 PoC 의 사후 검증으로 thesis_invalidated 의 outcome 예측력이 강하다는 게 나오면 그때 별도 결정한다. 그 전엔 advisory 만이 default 다.

이 두 원칙이 PoC design `2026-05-23-temporal-context-poc.md` §5 에 추가됐다.

## 12. 참조

- 5-15 사고 분석: `.ai/designs/2026-05-15-scout-overextension-guards.md` §1 ~ §2
- 5-17 두 결손 압축: `.ai/designs/2026-05-17-g-series-simplification.md` §1
- 5-22 결정론 코어 결정 기록: `.ai/decisions/2026-05-22-selection-architecture-decision.md`
- 5-22 commit: a65db2e (feat: 결정론 선정 코어 복원 — v2 quant.py 포팅, LLM codegen 은퇴)
- 5-23 Temporal Context PoC: `.ai/designs/2026-05-23-temporal-context-poc.md`
- 5-12 LLM 자율성 비전 폐기 + 5-23 재구성: 로컬 메모리 `project_vision_llm_autonomy_dropped.md`
- 글쓰기 가이드: AGENTS.md §글쓰기 가이드 + 글로벌 `communication-style.md`
