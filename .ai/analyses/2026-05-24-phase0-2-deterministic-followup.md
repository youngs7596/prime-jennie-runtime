# Phase 0 #2 후속 — 결정론 코어 기준 재검토

**작성**: 2026-05-24 (일)
**대상 비교 시점**: 옛 (4-17 ~ 5-22, LLM-at-core) vs 새 (5-22 ~ 현재, 결정론 코어, commit a65db2e)
**선행 글**: `.ai/analyses/2026-05-23-phase0-2-3-initial.md` (Phase 0 #2 1 차 분석)

## 1. 출발점과 이번 글의 한계

옛 1 차 분석은 outcomes 132 건 위에서 했는데 그 절대다수가 LLM-at-core 시절 산물이다. 결정론 코어가 5-22 13:12 KST 에 라이브 진입했으니 그 결론이 새 코어 위에서도 유효한지를 다시 보는 게 이번 글의 의도였다. 사용자 요청이 그 재검토였다.

결과부터 적자면 outcomes 기준의 직접 비교는 아직 불가능하다. 5-22 (금) 단 하루 가동 후 5-23 (토), 5-24 (일) 휴장이라 결정론 코어 시절 outcomes 가 0 건이다. position_sheets 15 건이 발행됐고 entry 진입은 일부지만, 완결 사이클은 다음 주 (5-26 ~ 5-30) 부터 쌓이기 시작한다.

그래도 발행 시점의 특성은 비교 가능하다. 옛 분석의 핵심 결론이 "entry 단계 문제 — SECTOR_MOMENTUM·EARNINGS_DRIFT 의 entry 신호 보강 우선" 이었으니, 결정론 코어가 발행한 시트의 entry 시점 특성을 옛 시절 손절 89 건과 같은 지표로 비교해 봤다.

## 2. strategy_tag 분포의 큰 변화

옛 LLM-at-core 시절의 손절 89 건과 결정론 코어 시절의 발행 15 건을 strategy_tag 로 보면 분포가 크게 다르다.

| strategy_tag | 옛 손절 89 건 | 결정론 발행 15 건 | 결정론 candidates 29 건 |
|---|---|---|---|
| SECTOR_MOMENTUM | 39 (43.8%) | 11 (73.3%) | 22 (75.9%) |
| EARNINGS_DRIFT | 38 (42.7%) | 1 (6.7%) | 4 (13.8%) |
| GAP_UP_REBOUND | 10 (11.2%) | 0 | 0 |
| MEAN_REVERT_RSI | 2 (2.2%) | 3 (20.0%) | 3 (10.3%) |

두 가지가 눈에 띈다. 결정론 코어가 SECTOR_MOMENTUM 으로 더 집중해서 배정하고, EARNINGS_DRIFT 와 GAP_UP_REBOUND 비중이 크게 줄거나 사라졌다. 이건 결정론 코어의 `_assign_strategy_tag` 어댑터가 단순 룰로 배정하기 때문이다 (v2 quant.py 에는 strategy_tag 자체가 없었고 v3 ScreeningCandidate 가 필수 필드라 추가된 어댑터). 어댑터의 룰이 어떤 신호를 EARNINGS_DRIFT 로 잡는지 다시 보는 게 별도 후속 작업이다 — 옛 LLM 이 EARNINGS_DRIFT 로 잡던 신호의 절반 이상이 결정론 코어에선 SECTOR_MOMENTUM 으로 흡수됐을 가능성이 크다.

옛 분석의 "SECTOR_MOMENTUM + EARNINGS_DRIFT 두 전략의 entry 신호 보강 우선" 결론이 결정론 코어 위에선 사실상 "SECTOR_MOMENTUM 의 entry 신호 보강 우선" 으로 좁혀졌다. SECTOR_MOMENTUM 의 비중이 75% 가 넘으니 한 전략의 entry 품질이 거의 전체 결과를 결정한다.

## 3. overextension 지표 비교 — 5-15 사고 결함이 일부 자연 해소

5-15 사고 분석에서 핵심 결함 중 하나가 "이미 너무 오른 종목 회피 가이드 없음" 이었다 (5-15 scout-overextension §1). 옛 손절 10 종목의 90% 가 60 일 고가 89% 이상에서 진입했다는 데이터가 근거였다. 결정론 코어가 이 지표에서 어떻게 다른지 봤다.

| 지표 | 옛 89 손절 entry | 결정론 코어 14 candidates entry |
|---|---|---|
| 60 일 고가 대비 평균 | 89.9% | 83.9% |
| 60 일 고가 89% 이상 비율 | 62.9% (56/89) | 37.5% (6/16) |
| 60 일 고가 95% 이상 비율 | 33.7% (30/89) | 25.0% (4/16) |
| 5 일 누적 수익률 평균 | +2.37% | +1.6% |

결정론 코어가 평균적으로 덜 상투에서 진입한다. 89% 이상 비율이 62.9% 에서 37.5% 로 절반 가까이 줄었다. 5-15 사고가 드러낸 "평가 방향 모멘텀 일변도" 결함이 v2 quant.py 의 7 팩터 (momentum 단독이 아니라 quality, value, technical, news, supply_demand, sector_momentum 의 가중합) 로 일부 자연 해소된 셈이다.

다만 표본 4 종목 (005930 삼성전자 100.0%, 000660 SK하이닉스 97.2%, 034730 SK 96.0%, 017670 SK텔레콤 95.2%) 이 60 일 고가 95% 이상에서 진입했다. 옛 시절보다 절대 수치는 줄었지만 "신고가 진입" 자체는 사라지지 않았다. 다음 주 거래 시작 시 이 네 종목이 어떻게 움직이는지가 결정론 코어의 첫 검증 사례다.

## 4. 옛 분석의 핵심 결론은 결정론 코어에서 어떻게 보아야 하는가

옛 분석은 세 가지 결론을 냈다.

**(a) 5% 고정 손절선은 문제가 아니다.** 손절 후 종목들이 시장보다 평균 -1.76 ~ -3.68% 더 떨어졌으니 손절선을 풀어 두면 손실이 더 커진다는 결론이었다. 이 결론은 손절 후 가격 추적 분석이라 결정론 코어 도입과 무관하다. 그대로 유효하다.

**(b) 진짜 문제는 entry 단계다.** 결정론 코어가 평균적으로 덜 상투에서 진입한다는 점은 §3 의 데이터가 보여줬다. 다만 신고가 진입은 사라지지 않았고, SECTOR_MOMENTUM 한 전략에 75% 가 몰리는 새 위험도 생겼다. 옛 결론을 그대로 가져오기엔 무리고, **SECTOR_MOMENTUM 의 entry 신호가 어떤 outcome 을 만드는지** 가 새 측정 대상이다. 5-22 발행 후보 14 종목 중 60 일 고가 95% 이상 4 종목이 다음 주 어떻게 움직이는지를 base case 로 보고, 한 달 누적 후 옛 손절 89 건과 같은 식의 overextension × outcome 매트릭스를 다시 짠다.

**(c) ATR 기반 동적 손절선의 한계 효용은 작다.** 손절 후 시장 대비 음의 alpha 가 1 ~ 3 일 후까지 이어지니 손절선 조정으로 살릴 게 별로 없다는 결론이었다. 이 부분도 손절 후 추적이라 그대로 유효하다.

## 5. 무엇이 새로 가능해졌는가

결정론 코어 도입으로 옛 분석에선 못 보던 두 가지가 가능해졌다.

첫째, **conviction-outcome 상관 분석이 결정론 코어 위에서 즉시 가능**하다. screening_candidates 29 건이 conviction 0.400 ~ 0.685 분포로 쌓이고 있다. 5-22 commit a65db2e 의 deterministic_scout.py:164 가 ma_score / 100 으로 발행한다. 옛 메모리의 "결정론 quant 가 conviction 발행 안 함" 진술은 정정됐다 (`project_selection_architecture_decision`).

둘째, **7 팩터 점수의 entry 시점 분포와 outcome 사이 상관**을 따로 볼 수 있다. screening_candidates.factors_json 에 momentum, quality, technical, sector_momentum, quant_total, ma_score 가 다 들어있다. 5-22 SECTOR_MOMENTUM 11 시트의 점수 패턴을 보면 momentum 8.0 ~ 11.4, quality 8.5 ~ 13.0, technical 5.5 ~ 8.0, sector_momentum 0 ~ 10 으로 다양하다. 어느 팩터가 outcome 을 가장 잘 예측하는지를 한 달 누적 후 측정 가능하다. 이건 옛 LLM-at-core 시절엔 불가능했던 분석이다 (LLM 이 매번 다른 코드로 점수를 만들었으므로 비교 기준점이 없었다).

이 두 분석이 5-23 master doc §10 의 첫 번째 우선순위 (Phase 0 #1 conviction-outcome script 준비) 와 같은 자리에 있다.

## 6. 다음 행동

5-29 까지 두 가지를 준비한다.

**conviction-outcome 상관 + 7 팩터 outcome 상관 SQL script 를 미리 짜 둔다.** screening_candidates.conviction × outcomes.pnl_pct join 으로 r1 (전체), r2 (strategy_tag 별), r3 (7 팩터 각각 × pnl_pct) 를 한 번에 본다. 분모는 promoted_to_sheet_id 가 채워진 candidates 만 (sheet 까지 간 것). 옛 시절 데이터로도 같은 script 가 돌아야 비교 가능 — LLM-at-core 시절 conviction 이 어떤 값으로 발행됐는지 확인이 필요하다 (확인 후 비교 표본 확정).

**60 일 고가 대비 진입 비율 추적을 일별 cron 으로 만든다.** 결정론 코어 발행 시점마다 daily_quant_scores 와 daily_prices 를 join 해서 자동 기록. 옛 89.9% / 89% 이상 62.9% / 95% 이상 33.7% 가 일별로 어떻게 변하는지 한 달 추적. 5-22 의 83.9% / 37.5% / 25% 가 한 달 평균에서 유지되는지가 첫 검증 지점이다.

Phase 0 #3 (Macro 게이트 보정 후속) 의 한 거래일 안 closed→open 역행 차단 가드는 이 작업과 독립이라 별도 시점에 진행한다.

## 7. 핵심 참고

옛 분석의 결론 (b) "진짜 문제는 entry 단계" 가 결정론 코어로 옮긴 후 의미가 바뀌었다는 점이 이번 검토의 가장 큰 발견이다. 결정론 코어가 평균적으로 덜 상투에서 진입하는 건 사실이지만, SECTOR_MOMENTUM 한 전략에 75% 가 몰리는 새 집중 위험이 생겼다. SECTOR_MOMENTUM 의 entry 신호 품질이 결정론 코어 시대 전체 outcome 의 거의 전부를 결정한다. 한 달 누적 후 같은 분석을 다시 돌릴 때 이 한 전략의 outcome 분포를 가장 먼저 본다.

표본 부족이라는 한계는 분명하지만, 결정론 코어 도입이 옛 결함 중 하나 (평가 방향 모멘텀 일변도) 를 일부 자연 해소했다는 점은 5-22 commit 의 가치를 보여 준다.

## 8. 참조

- 옛 1 차 분석: `.ai/analyses/2026-05-23-phase0-2-3-initial.md`
- 큰 그림 재정렬: `.ai/designs/2026-05-23-post-llm-at-core-realignment.md`
- 결정론 코어 commit: a65db2e (2026-05-22 13:12 KST)
- 5-22 결정 기록: `.ai/decisions/2026-05-22-selection-architecture-decision.md`
- 5-15 사고 분석 (overextension): `.ai/designs/2026-05-15-scout-overextension-guards.md` §1, §2
