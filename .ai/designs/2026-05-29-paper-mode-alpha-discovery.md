# prime-jennie v3 — Paper 모드 alpha 탐색 전환 설계

**작성일**: 2026-05-29
**상태**: 확정 (ground truth)
**대체**: LLM-at-core 시절 design doc 전부, "실계좌 alpha 자동매매" 비전
**선행 결정**: `.ai/decisions/2026-05-22-selection-architecture-decision.md` (결정론 코어 전환), `session-2026-05-27-0001.md` (paper outcomes v0 도입)

> 이 문서는 현재 prime-jennie v3 의 **목적과 경계**를 정의한다. 이후 모든 작업은
> 이 문서를 기준으로 한다. docs/ 와 .ai/ 안의 다른 문서가 이 문서와 충돌하면
> 이 문서가 우선한다.

---

## 1. 정체성

prime-jennie v3 = **paper 기반 alpha 탐색 실험실**.

- **자산 운용과 분리**. 실계좌는 운영자가 KODEX 200 + 직관으로 직접 운용한다.
  v3 는 실계좌에서 손을 뗀다. v3 가 운영자 자산을 매매하지 않는다.
- **LLM 의 위치는 매매 결정 라인 외부**. 매매 결정은 결정론 코어가 한다.
  LLM 은 두 곳에만 남는다 — (a) 상류 데이터 생성 (macro regime, 뉴스 감성),
  (b) 운영자 코파일럿 (분석·디버깅·문서화, 즉 Claude Code/대화).
- **목표는 증명**. "실계좌에 올릴 만한 alpha 가 있는가" 를 돈 리스크 없이
  paper 로 증명한다. 증명되면 그때 실계좌 승급을 재고한다.

---

## 2. 왜 이 전환인가

근거는 데이터다.

- 2025-11 이후 운영자 자산은 거의 늘지 않았다. 그 구조는 — v3 가 자산을
  깎으면 운영자가 멈추고 수익 종목을 직접 매수해 복구하고, 회복되면 다시
  v3 에 넘기는 사이클의 반복이었다. 순합이 자산 정체. v3 의 기여는 음수,
  운영자 수동 개입이 그것을 메워 겨우 0.
- v3 LLM 시기 (4-18 ~ 5-22) 실 매매 132 건은 평균 -2.24%/건, 승률 27%
  (`session-2026-05-25-0004.md`). 같은 종목 반복 매수로 손실을 키웠다
  (엘앤에프 8회 전패, HD현대일렉 5회 전패).
- 5-22 에 "검증된 v2" 가 허상으로 밝혀졌다. v2-native 는 2주만 라이브
  후 은퇴, 실적 35건 -34만원. 좋아 보이던 +3,019만원은 전부 v1-ETL.
  **어느 세대도 길게 검증된 적이 없다.**
- 실계좌로는 alpha 를 증명하기 전에 돈이 깎여 운영자가 멈춘다. 그래서
  검증이 매번 단절된다. paper 는 이 단절을 없애고, 풀 사이즈 표본을
  빠르게 모은다. 실계좌로는 평생 못 모을 표본을.

이 전환은 비전 폐기가 아니라 **비전을 증명 가능하게 만드는 경로**다.
증명이 통과 안 되면 (paper 가 인덱스를 못 이기면) 애초에 실계좌에
올릴 이유가 없었다는 뜻이고, 그건 paper 가 알려준 것이지 돈으로 배운 게
아니다.

---

## 3. 폐기된 비전 (명시)

향후 이 항목들로 회귀하지 않는다. 회귀 충동이 들면 이 절을 먼저 읽는다.

| 폐기 비전 | 폐기 시점 | 사유 |
|---|---|---|
| LLM-at-core (매 scout 코드 LLM 생성) | 5-22 | 138 run 전부 distinct code_hash, Jaccard 0.317 진동. 132건 -2.24% |
| multi-agent council debate | 4월 | 단일 LLM structured output 으로 단순화 |
| 실계좌 alpha 자동매매 | 5-29 | 자산 정체. paper 증명 후 재고 |
| 은퇴 후 방치 자율 운용 | 5-29 | 현 LLM 한계 (stateless·확률적·uncalibrated) 로 도달 불가 |

---

## 4. 현재 아키텍처 (5-22 이후)

```
slow_loop (시간당):
  macro gate (LLM, 바이너리 출력 open/closed + sizing)
    → deterministic scout (결정론 7팩터 + MA 평활 + 히스테리시스)
    → selection
    → publisher (position_sheets 영속 + Redis emit)

fast_loop (실시간 틱):
  position_sheets 소비 → tick 진입/청산 (exit_evaluator 9룰)

STOP 상태 (현재):
  시트는 position_sheets 에 매일 영속, Redis emit 만 차단
  → fast_loop 가 진짜 주문을 안 넣음
  → 분석·시트 발행은 그대로 (관측·시뮬레이션 데이터 보존)

paper outcomes (5-27 도입, jobs/paper_outcomes.py):
  발행 시트를 사후 시뮬레이션 (fast_loop 의 exit_evaluator 재사용)
  현재 v0: 일봉 4-tick, coverage='daily_only', overextension_exit skip

Coordinator (결정론):
  컴포넌트 간 cross-cutting policy 게이팅 (State Hub + Policy Engine)
```

선정 경로 LLM 호출 0회. macro·뉴스 감성은 상류 LLM 산출 *데이터*로서
결정론 스코어러가 입력으로 소비한다 (v2 와 동일 구조).

---

## 5. alpha 탐색 루프

paper 모드의 목적은 "가상 매매" 자체가 아니라 **alpha 를 찾는 작업을
돈 리스크 없이 빠르게 반복**하는 것이다.

```
1. paper 로 시스템 운영 → 매일 시트 발행 + outcomes 측정
2. 데이터 누적 (일주일~한 달) → 분석: 어떤 종목/전략/국면에서 이기고 지나
3. 가설 수립 (예: supply_demand 가중치가 대형주 쏠림을 만든다)
4. 코드 수정 — v3 안에서 in-place
5. 다시 paper 로 측정, 이전 baseline 과 비교  ← 실계좌에서 못 했던 단계
6. 반복
```

실계좌에서 못 돌린 건 5번이다. 매번 돈이 깎이면 멈추고 직관으로
복구해서 시스템 데이터가 끊겼고, baseline 이 매번 리셋됐다. paper 면
이 루프가 안 끊긴다.

**self-evolving 연결**: paper outcomes 가 Eval 의 ground truth 가 된다.
AI Coding Agent Harness 가 그 Eval 결과를 읽고 개선안을 생성·검증하면,
4번(수정)을 harness 가 보조하고 1번(측정)이 자동화되어 "측정 → 개선 →
재측정" 이 반쯤 자동으로 도는 자가진화 루프가 된다.

---

## 6. 측정 기준

- **벤치마크**: paper PnL vs KODEX 200 (069500) BUY & HOLD, 동일 기간·동일 시작 자본.
  절대수익이 아니라 인덱스 대비 초과분이 alpha.
- **선별 alpha**: 시트별 상대 비교. 진입가 모델 오차에 둔감하므로 v0 에서도 유효.
- **지표**: expectancy, 승률, 손익비, max drawdown.
- **국면 분해**: 5-25-0004 에서 확인됐듯 alpha 는 시장 국면 의존적이다
  (v2 시기 금융주 강세 → 5월 자동차·항공·통신 강세). 국면별로 분해해서 본다.

---

## 7. 개발 원칙

1. **in-place 전개**. 새 repo 금지. v3 안에서 키우고 줄인다. rewrite 는
   암묵지(축적된 edge case 처리)를 날리고 검증을 리셋한다. v3 는 본인이 짠
   코드, 실계좌에서 돌았음, Python 그대로 — rewrite 정당화 조건 셋 다 해당 안 됨.
2. **검증 연속성**. baseline 리셋 금지. 변경마다 이전 baseline 과 비교.
3. **거버넌스**. 새 결정/설계 commit 에 영향 자산을 함께 처리한다:
   깨진 docs → docs/archive/, 폐기된 .ai/designs·decisions → .ai/archive/,
   dead code → 같은/후속 commit 으로 삭제, "폐기된 비전" 절에 한 줄 추가.
4. **PoC 는 격리**. 새 아이디어는 production 이 아니라 별도 실험 폴더에서
   검증하고, 검증되면 그 컴포넌트만 strangler 로 v3 에 이식한다.

---

## 8. 이 설계가 닫는 질문 / 여는 질문

**닫는 질문**:
- v3 가 운영자 자산을 매매하는가 → 아니오 (paper 만).
- LLM 이 매매 결정을 하는가 → 아니오 (결정론 코어가).
- 다음 불만족 시 새 repo 로 가는가 → 아니오 (in-place).

**여는 질문 (paper 데이터로 답할 것)**:
- 결정론 코어 + Coordinator policy 가 KODEX 200 을 의미 있게 이기는가?
- 못 이기면 underperform 의 원인 layer 는 어디인가 (선별/타이밍/exit)?
- macro 의 LLM 게이트가 alpha 에 기여하는가, 결정론 룰로 대체 가능한가?
