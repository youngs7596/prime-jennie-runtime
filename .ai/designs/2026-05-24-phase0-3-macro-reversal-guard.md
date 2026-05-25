# Phase 0 #3 — Macro 게이트 closed → open 역행 차단 가드

**작성**: 2026-05-24
**범위**: Macro 게이트 판단 layer 의 결정론 보강 한 가지
**연결**: `.ai/designs/2026-05-23-post-llm-at-core-realignment.md` master doc, `.ai/analyses/2026-05-23-phase0-2-3-initial.md` 1 차 분석

## 1. 배경

5-12 외부 검토에서 Phase 0 의 세 과제로 conviction-outcome 상관 (#1), 손절 진단 (#2), Macro 게이트 보정 (#3) 이 지정됐다. 이 글은 #3 의 결정론 가드 한 가지를 설계한다. master doc §9 의 "active" 항목이고, 다른 도메인을 건드리지 않는다.

5-15 사고의 사실관계는 `2026-05-23-phase0-2-3-initial.md` §3.3 에 정리돼 있다. 같은 거래일 안에서 macro_runs 가 closed 로 전환된 뒤 open 으로 되돌아간 사례가 한 차례 있었다. 이번에 PG 데이터로 다시 확인했다 (KST):

| 시각 | gate | size | KOSPI | LLM 결론 요지 |
|---|---|---|---|---|
| 11:40 | open | 0.75 | -3.20% | closed 조건 5 개 중 충족 없음 |
| 12:11 | closed | 0 | -3.54% | 섹터 전염 #3 + 지정학 #1 충족 |
| 13:11 | closed | 0 | -3.69% | 섹터 전염 #3 + 지정학 #1 충족 |
| 13:36 | closed | 0 | -4.83% | 주요 섹터 동시 -5% 충족 |
| **13:42** | **open** | **0.75** | **-4.46%** | **closed 조건 5 개 검토 결과 미충족** |
| 14:12 | closed | 0 | -5.47% | 섹터 전염 #3 + 시스템 이벤트 #5 |

13:42 의 open 역행이 직전 13:36 closed 와 6 분 차이다. KOSPI 는 그 사이에 회복된 게 아니라 -4.83% → -4.46% 로 0.37 포인트 미세 회복했을 뿐이다. LLM 이 같은 정보로 정반대 결론을 낸 것이다. 그 윈도가 30 분 만에 14:12 에서 다시 closed 로 되돌아갔다.

이번 PG 점검에서 13:30 ~ 14:30 KST 사이의 executions 가 0 건임을 확인했다. 즉 이 8 분 역행 윈도 자체에서 매수가 들어가진 않았다. 다만 가드의 필요성은 같다 — 다음에 비슷한 비결정성이 나타날 때 그 윈도에 fast-loop 이 시초 매수를 들어가지 않으리란 보장이 없다.

## 2. 비결정성의 본질

LLM Macro 게이트는 KOSPI 변동, 섹터 전염, 지정학 뉴스, 시스템 이벤트, 시장 휴장 다섯 가지 조건을 보고 open/closed 를 판단한다. 동일 입력에서도 LLM 출력은 sampling 분포에 따라 흔들린다. minyoung-mah 학습이 분명하게 말했다 — prompt 로 LLM 의 결정론을 제어하려 하지 말고, 결정론은 별도 layer 에 두라. master doc §3 도 같은 원칙을 5-15 scout-overextension doc 에서 인용했다 ("모든 enforcement 는 결정론 코드 layer").

5-15 사례는 그 원칙을 보강해야 한다는 것을 보여준다. closed 조건을 만족해 한 번 게이트가 닫혔다는 사실은 LLM 의 출력 노이즈로 되돌릴 수 있는 결정이 아니다. 그 자체가 결정론 layer 의 단방향 잠금 대상이다.

## 3. 옵션 비교

세 가지를 비교한다.

**옵션 A — 거래일 단방향 잠금**
같은 거래일 안에서 macro 가 closed 로 전환된 뒤에는, 후속 macro_runs 의 LLM 출력이 open 이라도 결정론 layer 가 closed 로 덮어쓴다. 다음 거래일 시초부터 정상 LLM 판단을 다시 신뢰한다.

장: 가장 단순하다. 코드 한 줄로 표현되고, 추가 임계값을 사전에 정할 필요가 없다. 5-15 의 8 분 역행 같은 사례를 100% 차단한다.
단: 짧은 패닉 + 같은 날 빠른 회복 패턴에서 매수 기회를 막는다. 다만 그 매수 기회의 기대값이 5-15 같은 사고를 견뎌낼 만큼 크다는 근거는 없다.

**옵션 B — Cooldown 윈도**
closed 전환 뒤 N 시간 (예: 30 분, 1 시간) 안에는 open 역행을 차단. N 이 지나면 LLM 판단을 다시 신뢰.

장: 짧은 패닉 후 진짜 회복 패턴은 cooldown 만료 후 자연 open. 옵션 A 보다 덜 보수적.
단: N 의 사전 결정이 어렵다. 5-15 사례는 8 분 역행 + 30 분 후 다시 closed 라 N=30 분이면 차단되지만 N=2 시간이면 14:12 의 정상 closed 도 막힌다. 데이터 한 사례에서 N 을 정하는 건 single-day overfit 위험 (`feedback_single_day_overfit.md`).

**옵션 C — 정량 조건 동반**
closed 전환 뒤 open 으로 풀리려면 KOSPI 가 closed 시점 대비 X% 이상 회복 같은 정량 조건을 만족해야 함.

장: 시간이 아니라 회복 신호 기반.
단: KOSPI 만으론 부족하고 다른 지표 (섹터 전염, 변동성) 까지 끌어오면 결정론 layer 가 LLM Macro 본래 판단을 흉내내게 됨. 그건 minyoung-mah 학습이 말한 "결정론 layer 에 LLM 의 일을 떠밀지 마라" 와 충돌. 또 5-15 13:42 의 open 역행이 KOSPI 회복과 무관 (-4.83% → -4.46%) 했다는 사실은, LLM 이 정량과 어긋난 판단을 한다는 증거 — 정량 조건이 LLM 판단을 신뢰 가능하게 만들지 않음.

## 4. 결정

옵션 A 권장. 거래일 단위 단방향 잠금.

이 결정의 정신은 다음과 같다. 한 번 closed 가 켜졌다는 사실은 그날의 시장 상태가 "매수 위험을 새로 무릅쓸 시점이 아니다" 라는 판단을 이미 내렸다는 것이다. LLM 의 다음 호출이 같은 시장 상태를 다르게 본다고 해서 그 판단을 되돌릴 권한을 LLM 에게 주지 않는다. 다음 거래일 시초의 새 컨텍스트에서 다시 판단한다.

5-15 사례 한 건만 보고 정하는 결정이 아니다. 결정론 layer 와 LLM layer 의 경계를 그리는 결정이다. minyoung-mah 학습과 master doc §3 의 원칙을 macro 도메인에 그대로 적용한 결과다.

## 5. 구현

### 5.1 위치

`prime_jennie_runtime/slow_loop/macro/post_processor.py` 가 LLM macro 출력에 후처리 가드를 거는 모듈이다. 기존에 `auto_override` (KOSPI 20d vol 임계 초과 시 강제 closed) 같은 가드가 거기 있다. 이번 가드도 같은 layer.

### 5.2 동작

같은 거래일 (KST 09:00 ~ 15:30, 즉 시초 ~ 종가 구간) 안에서:

1. 직전 macro_runs 에서 gate=closed 인 row 가 있는지 확인 (auto_override 와 LLM 자체 closed 둘 다 포함).
2. 있으면 현재 LLM 출력이 open 이라도 closed 로 덮어쓴다. size_multiplier 도 0 으로 강제.
3. `metadata_json` 에 `reversal_guard: true` + `original_gate: open` + `latched_from_macro_run_id: <id>` 기록. LLM 원본 판단은 reasoning 필드에 그대로 남긴다 (사후 분석용).
4. 다음 거래일 시초 (KST 09:00 이후 첫 호출) 부터는 가드 비활성.

15:30 ~ 다음 09:00 의 장외 시간은 가드 적용 범위 밖이다. 장외엔 매매가 일어나지 않고, 다음 거래일 macro 판단은 새 컨텍스트라서.

### 5.3 코드 위치 추정

`post_processor.py` 의 후처리 흐름 안에서 `auto_override` 다음에 한 단계 추가. 함수 한 개 (이를테면 `_apply_reversal_latch`) + 호출 한 줄. 직전 closed 조회는 PG `macro_runs` 테이블의 동일 거래일 row WHERE gate='closed' ORDER BY generated_at DESC LIMIT 1.

테스트는 단순하다. monkeypatch 로 직전 closed 가 있는 상태와 없는 상태 둘을 만들고, LLM 출력이 open 일 때 가드 동작 확인. 통합 테스트는 `tests/slow_loop/macro/test_post_processor.py` 에 한 묶음 추가.

### 5.4 비활성화 env

`MACRO_REVERSAL_GUARD_DISABLED=1` 같은 env 로 끌 수 있게 한다. `MACRO_AUTO_OVERRIDE_DISABLED` 와 같은 패턴. 운영 점검 시 일시 해제용. 평상시에는 활성.

## 6. Pre-flight 점검

작업 전에 다음을 확인한다.

- `post_processor.py` 의 현 구조 (auto_override 가 어떻게 적용되는지)
- macro_runs 테이블의 동일 거래일 SELECT 가 LLM 호출 직전에 들어가도 무방한지 (latency 비용 거의 0 — PG 로컬 1 row 조회)
- `feedback_consumer_regression_check.md`: 결정 변경 시 macro 결과를 소비하는 모든 곳 (fast-loop sizer 등) 이 새 동작과 호환되는지 grep 확인. closed 의 의미가 바뀐 게 아니라 closed 의 latch 가 추가된 것이라 호환 깨질 가능성은 낮음.

## 7. 다음 단계

설계 본문은 여기서 끝. 구현은 이번 세션 이어서 갈지, 별도 세션에서 짤지 사용자 결정.

5-25 (월) ~ 5-29 (금) 한 주의 운영 데이터가 쌓이는 동안 이 가드가 한 번이라도 발동하면 사후 분석 자료가 된다. metadata_json 의 `reversal_guard: true` row 가 있는지 주기적으로 본다. 없으면 평상시 비활성 동작, 있으면 LLM 의 원본 판단과 가드 발동 시점을 비교해 가드 정당성을 다시 점검한다.

## 8. 참조

- master doc: `.ai/designs/2026-05-23-post-llm-at-core-realignment.md`
- 1 차 분석: `.ai/analyses/2026-05-23-phase0-2-3-initial.md` §3.3 (5-15 사례)
- 시작점 doc: `.ai/designs/2026-05-15-scout-overextension-guards.md` (모든 enforcement 는 결정론 layer 원칙 출처)
- 학습: `feedback_prompt_control_limit.md`, `feedback_single_day_overfit.md`, `feedback_audit_layers.md`
