# strategy_tag 어댑터 재검토

**작성**: 2026-05-24 (일)
**대상 코드**: `prime_jennie_runtime/slow_loop/scout/deterministic_scout.py` 의 `_assign_strategy_tag` (line 132 ~ 152)
**전제 commit**: a65db2e (5-22 결정론 선정 코어 복원)
**선행 분석**: `.ai/analyses/2026-05-24-phase0-2-deterministic-followup.md` §2

## 1. 왜 다시 보게 됐는가

Phase 0 #2 후속 분석에서 결정론 코어가 5-22 단 하루에 발행한 strategy_tag 분포가 옛 LLM-at-core 시절과 크게 다르다는 점이 드러났다. SECTOR_MOMENTUM 이 옛 43.8% 에서 75.9% 로 증가하고 EARNINGS_DRIFT 는 42.7% 에서 13.8% 로 줄고 GAP_UP_REBOUND 는 11.2% 에서 0 으로 사라지고 MEAN_REVERT_RSI 는 2.2% 에서 10.3% 로 늘었다. 표본이 작아 결론을 내릴 수는 없지만 분포 변화 자체가 명백해서 어댑터 룰을 다시 봐야 했다.

이번 글의 목적은 두 가지다. 어댑터 룰과 옛 LLM prompt 가이드 사이의 불일치를 짚는다. 그 위에서 변경 옵션을 비교하고 어디까지 즉시 정정할지 결정한다.

## 2. 어댑터의 현재 룰과 옛 prompt 정의

어댑터의 결정 트리는 단순하다.

- RSI ≤ 35 라면 MEAN_REVERT_RSI 로 분류한다.
- 그게 아니면서 eps_revision_pct 가 5 이상이면 EARNINGS_DRIFT 로 분류한다.
- 둘 다 아니면 SECTOR_MOMENTUM 으로 분류한다 (default 버킷).
- GAP_UP_REBOUND 는 일봉 스코어러에서 명시적으로 배정하지 않는다.

옛 LLM-at-core 시절 prompt 가이드는 네 가지 strategy_tag 를 자연어로 정의했다.

- GAP_UP_REBOUND 는 갭상승과 단기 모멘텀이다. 1 ~ 3 일 내 진입.
- SECTOR_MOMENTUM 은 섹터 강세 동조이고 중기 추세다.
- EARNINGS_DRIFT 는 실적 발표 직후 PED (Post-Earnings Announcement Drift) 이고 1 ~ 2 주 보유.
- MEAN_REVERT_RSI 는 과매도 (RSI ≤ 30) 후 반등 후보다.

옛 prompt 에는 추가 가이드도 있었다. "strategy_tag 가 한쪽으로 쏠리니 호재 이벤트군을 동등 취급" 과 "5 개 이상 후보 산출 시 최소 2 종 이상의 strategy_tag 사용" 이라는 사용자 지시가 prompt 안에 박혀 있었다. 즉 옛 시대에는 자연어로 분류 의미를 정의하고, 분산을 prompt 로 강제하는 형태였다.

## 3. 불일치 네 가지

| # | 옛 prompt | 어댑터 | 영향 |
|---|---|---|---|
| 1 | RSI ≤ 30 | RSI ≤ 35 | MEAN_REVERT_RSI 비중 2.2% → 10.3% 로 5 배. 임계가 5 포인트 느슨해서 과매도 아닌 종목까지 이 분류로 들어옴 |
| 2 | 실적 발표 직후 드리프트 윈도우 (이벤트 시점) | eps_revision_pct ≥ 5 (컨센서스 데이터) | 옛 정의는 발표 이벤트 시점 신호, 어댑터는 컨센서스 상향 신호. 두 신호의 출처와 빈도가 다름. EARNINGS_DRIFT 가 거의 못 배정됨 (4 건) |
| 3 | 갭상승 / 단기 모멘텀 (장중 갭) | 일봉 스코어러는 배정하지 않음 (명시적 결정) | 옛 시절 손절 89 건 중 10 건이 이 태그. 결정론 코어 시대 0. 일봉 데이터에서도 어제 종가 vs 오늘 시가 갭은 잡을 수 있음 |
| 4 | 섹터 강세 동조 / 중기 추세 (의미 라벨) | RSI > 35 + EPS revision < 5 인 모든 후보 (잔여 버킷) | 75.9% 집중의 진짜 원인. 분류 라벨이 아니라 default fallback |

네 가지 중 #1 만 어댑터의 명백한 버그 (prompt 가이드 숫자 자체와 다름) 이고, #2 ~ #4 는 결정론 룰로 LLM 의 자연어 판단을 표현하는 과정에서 생긴 정의 변경이다.

## 4. 더 깊은 문제 — strategy_tag 가 exit 정책 라우터다

`prompts.py:261 ~ 264` 가 strategy_tag 별 exit 정책 default 를 정의한다. EARNINGS_DRIFT 는 hold_days 5 ~ 10 일 + fixed_sl 5%, SECTOR_MOMENTUM 은 trailing 6%/3% + 10 일, MEAN_REVERT_RSI 는 fixed_tp 4% + 3 일, GAP_UP_REBOUND 는 trailing 5%/3% + 3 일. 옛 LLM 시대에는 strategy_tag 의 의미 (왜 사는가) 와 exit 정책 (어떻게 끊는가) 이 일관됐다.

결정론 코어 시대에는 이 일관성이 깨졌다. 어댑터의 default 버킷 (SECTOR_MOMENTUM 75%) 안에는 실제로 섹터 강세 동조 시그널이 약한 후보가 다수 섞여 있다. 5-22 발행 11 SECTOR_MOMENTUM 시트의 sector_momentum 점수가 0 ~ 10 으로 다양했고, 0 점인 034730 (SK) 까지 SECTOR_MOMENTUM 라벨을 받고 trailing 6%/3% + 10 일 exit 정책에 묶였다.

이건 분류 정확도 문제가 exit 정책 적정성 문제로 그대로 옮겨가는 구조다. 어댑터 룰 정확도를 높이는 것과 exit 정책의 strategy_tag 의존을 줄이는 것이 같은 줄기다.

## 5. 변경 옵션 네 가지

### (1) strategy_tag 자체를 1 종 (DETERMINISTIC) 으로 줄인다

가장 깨끗하다. exit 정책도 단순화한다 — 예를 들어 모든 시트에 trailing 5%/3% + 7 일 default. 단점은 옛 시대 strategy_tag 별 outcome 분포와의 비교 가능성을 한 번에 잃는다는 점이다. 5-15 사고가 SECTOR_MOMENTUM + EARNINGS_DRIFT 에 손절이 집중됐다는 데이터에서 시작했는데, 그 비교축을 없애면 결정론 코어의 outcome 분포가 옛 시대와 어떻게 다른지를 측정할 라벨이 사라진다.

### (2) 어댑터 룰을 옛 prompt 정의에 맞춰 정정한다

RSI 임계 35 → 30, EARNINGS_DRIFT 정의를 "실적 발표 이벤트 + EPS revision" 으로, GAP_UP_REBOUND 를 일봉 갭 (어제 종가 vs 오늘 시가) 으로 추가한다. LLM 자연어 판단을 결정론 룰로 흉내내는 셈이다. 5-15 사고가 학습시킨 LLM 의 위험 (overextension, 같은 패턴 반복) 을 결정론 룰이 다시 만들 가능성이 단점이다. 자연어 판단은 LLM 이 정교하지만 결정론 룰로 정교하게 흉내내려면 임계값과 조건 분기가 폭주한다.

### (3) strategy_tag 어댑터를 제거하고 exit 정책을 score 기반으로 재설계한다

ma_score 70 이상이면 trailing, 70 미만이면 fixed 같은 식. 가장 큰 변경이고 5-29 까지 일정에 안 맞는다. 5-29 이후 Temporal Context PoC 의 Fact layer (7 팩터 시계열 누적) 와 같이 다루는 게 자연스럽다.

### (4) 현 상태 유지하되 strategy_tag 를 사실상 측정 라벨로 쓴다

exit 정책의 strategy_tag 의존을 줄이고 별도 layer 에서 score 기반으로 결정한다. 옛 LLM 시대 비교 가능성은 유지한다. 즉시 가능한 정정은 어댑터의 명백한 버그 (RSI 35 → 30) 만이다.

## 6. 결정 — (4) 현 상태 + (2) 한정 정정

5-29 까지 일정에 맞고 위험이 가장 적은 길이다. 다음과 같이 진행한다.

**즉시 정정**: RSI 임계 35 → 30. 어댑터 한 줄 변경 + 테스트 갱신 + commit. 이건 prompt 가이드 자체에서 명시한 숫자와 다른 명백한 버그라 데이터 누적을 기다릴 이유가 없다.

**한 달 누적 후 결정**: #2 (EARNINGS_DRIFT 정의), #3 (GAP_UP_REBOUND 일봉 갭 배정), #4 (default 버킷의 의미 모호) 는 결정론 코어 outcome 표본 (5-26 ~ 6-23 누적) 위에서 strategy_tag 별 손절률과 7 팩터 점수 분포를 다시 본 다음에 결정한다. 옛 시절 strategy_tag 별 손절 87% (SECTOR_MOMENTUM + EARNINGS_DRIFT) 패턴이 결정론 코어에서 어떻게 변하는지가 어댑터 룰 변경의 근거가 된다.

**5-29 이후 별도 논의**: exit 정책의 strategy_tag 의존을 줄이는 큰 줄기는 Temporal Context PoC 의 Fact layer 와 묶어 그 시점에 같이 다룬다. 7 팩터 시계열 누적이 exit 정책의 입력이 되는 자연스러운 형태로 갈 가능성이 크다.

## 7. RSI 임계 정정의 근거

RSI 임계가 14 일 기간이고 임계 5 포인트 차이가 어떤 의미인지는 RSI 분포의 평균-분산 특성에서 본다. RSI 35 는 약세 구간이지만 "과매도" 라고 부르기엔 애매하다. 통상 30 이 과매도 기준선이다. 옛 prompt 가이드가 30 으로 명시한 것도 그 통념이다.

어댑터가 35 로 느슨하게 잡으면 약세지만 과매도는 아닌 종목까지 MEAN_REVERT_RSI 로 분류되고, 그 종목들이 MEAN_REVERT_RSI 의 exit 정책 (fixed_tp 4% + fixed_sl 3% + 3 일 짧은 보유) 을 받는다. 짧은 보유와 좁은 tp/sl 은 진짜 과매도 반등에는 맞지만 약세 추세에는 잡혀 손절될 가능성이 큰 정책이다. 즉 #1 의 영향이 분류 정확도뿐 아니라 exit 정책 적정성에도 직접 닿는다.

5-22 후보 14 종목 중 MEAN_REVERT_RSI 3 종 (000250, 035720, 051910) 의 60 일 고가 대비 비율이 28.8% / 81.6% / 79.5% 로 다른 카테고리보다 낮은데 5 일 누적 수익률은 -8.51% / -5.00% / -6.95% 다. 약세 추세 종목이라는 신호다. RSI 30 임계로 바꾸면 이 중 일부가 SECTOR_MOMENTUM 으로 빠지면서 다른 exit 정책을 받게 된다.

## 8. 측정 plan — 한 달 후 어댑터 재평가

5-26 ~ 6-23 (대략 22 거래일) 누적 후 다음 네 지표를 본다.

- strategy_tag 별 발행 건수와 발행률 (5-22 의 분포가 한 달 평균에서도 유지되는지)
- strategy_tag 별 손절률 (옛 시대 SECTOR_MOMENTUM + EARNINGS_DRIFT 87% 집중이 결정론 코어에서 어떻게 변하는지)
- default 버킷 SECTOR_MOMENTUM 안의 sector_momentum 점수 0 인 종목 비율 (라벨이 setup 과 분리된 정도)
- EARNINGS_DRIFT 4 건이 적정 분류였는지 outcome 으로 확인 (eps_revision_pct 신호가 의미 있는지)

이 네 지표가 어댑터 룰 #2 ~ #4 변경의 근거 데이터가 된다. 그 시점에 (2) 의 추가 정정, (1) 의 단순화, (3) 의 score 기반 재설계 중 하나로 갈지 결정한다.

## 9. 참조

- 옛 prompt 가이드: `prime_jennie_runtime/slow_loop/scout/prompts.py:209 ~ 212`, `:261 ~ 264`
- 어댑터 본문: `prime_jennie_runtime/slow_loop/scout/deterministic_scout.py:132 ~ 152`
- Phase 0 #2 후속: `.ai/analyses/2026-05-24-phase0-2-deterministic-followup.md`
- 큰 그림 재정렬: `.ai/designs/2026-05-23-post-llm-at-core-realignment.md`
- 5-15 사고 — strategy_tag 분산 강제 학습: `.ai/designs/2026-05-15-scout-overextension-guards.md` §1
