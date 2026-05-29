# 작업 지시서 — 2026-05-29 Paper 모드 전환 1차

**대상**: prime-jennie-runtime `main` 에서 작업하는 Claude Code
**선행 문서**: `prime-jennie-v3-paper-mode-design.md` (먼저 읽을 것)

## 공통 전제

- **in-place 진행**. 새 repo 금지. v3 안에서 변경한다.
- **각 묶음은 별도 commit**. Conventional Commits. 테스트 없는 commit 금지.
- **매매 영향 0**. v3 STOP 유지. 이 지시서의 모든 작업은 paper/문서/정리이며
  실계좌 매매 경로를 건드리지 않는다.
- commit 전 `ruff format . && ruff check .`.
- 각 P 작업 시작 전 현재 코드를 직접 확인하고, 이 지시서의 가정(파일 경로,
  dead 여부)이 맞는지 검증한 뒤 진행할 것. 틀리면 멈추고 보고.

---

## P0 — 정체성 확정 (문서, 1 commit)

**목적**: 이후 모든 작업의 기준점을 리포에 박는다.

1. `prime-jennie-v3-paper-mode-design.md` 를 `.ai/designs/2026-05-29-paper-mode-alpha-discovery.md` 로 커밋.
2. `README.md` 첫 단락 교체. 현재 "자가진화 KOSPI/KOSDAQ 트레이딩 시스템" 류 →
   "**paper 기반 alpha 탐색 실험실. 실계좌 자산 운용과 분리. LLM 은 매매 결정
   외부, 운영자 코파일럿.**" (디자인 문서 §1 요약)
3. `AGENTS.md` 수정:
   - "공유 스펙 (변경 시 stop-the-world)" 목록에서 `docs/SCOUT_CODE_GENERATION.md` **제거**.
   - "## 폐기된 비전" 섹션 신규 추가 (디자인 문서 §3 표 그대로).
   - "## 결정 변경 룰" 섹션 신규 추가 (디자인 문서 §7-3).
   - 리포 경계 설명의 "v3 실행 엔진" → "paper alpha 탐색 엔진" 으로 정정.

**commit**: `docs: prime-jennie v3 정체성을 paper alpha 탐색으로 확정`

---

## P1 — 문서 burn-down (1 commit)

**목적**: LLM-at-core 시절 문서가 ground truth 행세하는 상태를 끝낸다.

1. `mkdir -p docs/archive` 후 `git mv`:
   - `docs/SCOUT_CODE_GENERATION.md`
   - `docs/PHASE2_PLAN.md`
   - `docs/PHASE_2_10_UTILITIES_INVENTORY.md`
   - `docs/PHASE_2_13_COMPLETE.md`
   - `docs/prime_jennie_v3_phase0_design.md`
2. `mkdir -p .ai/archive` 후 LLM-at-core 시절 분석/결정을 이동.
   기준: 5-22 이전 작성 + 결정론 코어 전환으로 무효화된 것.
   (`.ai/reviews/2026-05-22-llm-at-core-review-request.md` 같이 결정 근거로
   남길 가치 있는 건 유지. 판단 애매하면 유지하고 보고.)
3. `docs/MACRO_GATE_SPEC.md` 는 **유지**하되 헤더에 한 줄 추가:
   "macro 는 바이너리 게이트 (open/closed + sizing). 출력은 결정론 스코어러의
   입력 데이터로만 쓰인다. 매매 결정 권한 없음."
4. `.ai/designs/2026-05-14-agent-coordinator.md` 헤더에 갱신 표시:
   "5-29 paper 모드 전환: Coordinator 는 '결정론 컴포넌트 간 cross-cutting
   policy 게이팅' 으로 재정의. 'LLM agents 추천 검증' 문구는 Scout 결정론화로
   부분 무효. 본문 정합은 cooldown/dedup policy 구현 시 갱신."

**commit**: `docs: LLM-at-core 시절 문서 archive 이동 + macro/coordinator 정합`

---

## P2 — Scout LLM 잔재 코드 제거 (commit 2~3개로 분리)

**목적**: 결정론 코어와 동거 중인 LLM-at-core 잔재를 제거해 ground truth 를
코드에서도 단일화한다.

**제거 대상** (deterministic_scout.py 가 import 안 함을 확인했음):
```
slow_loop/scout/prompts.py
slow_loop/scout/code_loop.py
slow_loop/scout/code_hasher.py
slow_loop/scout/validators.py
slow_loop/scout/screening_stub.py
slow_loop/scout/role.py
```

**순서**:
1. `slow_loop/pipeline.py` 의 import/호출 검증:
   - `compute_code_hash` (line ~59), `ScreeningInvoker` (~67), `validate_candidates` (~68)
   - 이 셋이 deterministic scout 경로 밖(persist/logging 등)에서 실제로 쓰이는지
     grep 으로 사용처 전수 확인. 안 쓰이면 dead import → 제거. 쓰이면 결정론
     대체 후 제거. **쓰임이 남아 있으면 멈추고 보고.**
   - `_scout_pipeline`, `StaticPipeline` scout 분기 등 LLM-at-core 전제 코드 정리.
2. 위 6개 파일 삭제.
3. 끊어진 import 정리 (다른 모듈에서 참조하는 곳 grep 후).
4. 테스트 정리:
   - stale: `test_bar_engine::test_indicator_provider_adapter`,
     `test_prompts_outcomes::test_prompt_version_bumped_*`
   - skip: `test_pipeline_e2e_real.py` 의 은퇴 codegen-sandbox 경로 3건 → 삭제/재작성
   - 삭제 파일을 테스트하던 케이스 제거.

**검증 게이트**:
- 전체 suite pass.
- `screening_executor/` 의 결정론 경로는 건드리지 않았는지 확인.
- (가능하면) 로컬에서 deterministic scout run 1회 dry-run, 후보 산출 정상 확인.

**commit 예시**:
- `refactor: pipeline 에서 LLM-at-core dead import/분기 제거`
- `refactor: scout LLM codegen 잔재 6파일 삭제`
- `test: 은퇴한 codegen 경로 테스트 정리`

---

## P3 — paper outcomes v0 → v1 (commit 분리)

**목적**: 측정 정밀도를 올려 alpha 신호를 신뢰 가능하게 만든다.
(작업 상세는 `jobs/paper_outcomes.py` 헤더의 "v1 계획" 주석에 이미 있음)

1. `minute_prices` 어댑터 추가 + 1분봉 RSI(14) 계산 (`calculate_rsi` 재사용).
2. `overextension_exit` 룰 활성화 — 1분봉 보유 종목 (시총 top30 + 워치리스트)은
   `tick.rsi_1m` 을 실제 값으로 채워 evaluator 가 평가하게.
3. 1분봉 있는 종목은 `coverage='full'` 로 메타 기록 (없으면 'daily_only' 유지).
4. `scale_out` portion 별 weighted exit_price 정밀 처리 (v0 는 전량 청산 근사).
5. 테스트 추가: overextension 매칭, full/daily_only coverage 분기, scale_out weighted.

**검증 게이트**:
- `jobs/` 테스트 pass.
- 6-1 (월) 첫 cycle 에서 top30/워치리스트 종목이 `coverage=full` 로 측정되는지 확인.

**commit**: `feat: paper outcomes v1 — 1분봉 어댑터 + overextension + scale_out 정밀화`

---

## P4 — benchmark dashboard (commit 분리)

**목적**: alpha 판정 기준선(KODEX 200)을 항상 나란히 본다.

1. `dashboard/routers/` 에 paper outcomes 라우터 신규 (또는 `portfolio.py` 확장).
2. paper 누적 PnL vs KODEX 200 (069500) BUY & HOLD 비교. 동일 기간·동일 시작 자본 가정.
3. 요약 카드: expectancy, 승률, 손익비, max drawdown, 인덱스 대비 초과분.
4. (선택) 국면 분해 뷰 — 기간 슬라이스별 alpha.

**검증 게이트**: dashboard 라우터 테스트 + 수동 렌더 확인. 매매 영향 0.

**commit**: `feat: dashboard 에 paper PnL vs KODEX200 benchmark 추가`

---

## 이번 지시서 범위 밖 (다음 사이클)

여기 적힌 건 지금 하지 말 것. 6-1 paper 데이터를 본 뒤 결정한다.

- **Coordinator cooldown/dedup policy**: 같은 종목 손절 후 N일 재진입 차단.
  5-25-0004 의 "같은 종목 반복 매수" 결함의 근본 해법. (selection 의 hysteresis
  와 별개 — hysteresis 는 점수 진동 억제, cooldown 은 손절 이력 기반 차단.)
- **macro LLM → 결정론 룰 게이트 전환 검토**: paper 에서 macro 게이트가 alpha 에
  기여하는지 측정한 뒤 판단. 기여 없으면 KOSPI vol / USD-KRW / VIX / 외인 순매도
  정량 임계값 룰로 대체 (5-24 Phase 0 #3 가드 연장선).
- **레벨 2 paper broker**: KIS gateway 에 paper mode. v1 측정 정밀도가 부족하다고
  판단될 때만. 처음부터 만들지 말 것 (over-engineering).
- **supply_demand 정의 재설계 (b안)**: 절대 거래대금 → 거래대금 증가율 기반
  (5-25-0004 §2.5). 외부 데이터 확인 필요, paper 측정으로 효과 검증.

---

## 권장 실행 순서

P0 → P1 → P2 → P3 → P4. P0/P1 은 한 세션에 가능 (문서 작업).
P2 는 검증 신중히 (commit 분리). P3/P4 는 6-1 첫 데이터 일정에 맞춰.
