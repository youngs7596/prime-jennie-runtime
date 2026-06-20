# AGENTS.md — user_directed_dca

운영자가 텔레그램으로 무장한 종목을 매 영업일 정해진 시각(기본 14:00, 캠페인별
execute_at_kst) 시장가로 buy-only 분할매수하는 독립 모듈. 명세 + 민지 rationale
(2026-06-20), 설계 노트 `.ai/designs/2026-06-20-user-directed-dca.md`. 집행 시각은
백테스트로 14:30→14:00 변경 (`.ai/analyses/2026-06-20-dca-entry-time.md`).

## 정체성 (바꾸기 전에 읽을 것)

- **사람-승인 매매의 연장**: 사람이 무장 시점에 [종목·배분·총액·슬라이스수·간격·집행시각]을
  되읽고 "확인"한다. 이후 감시·집행은 결정론 tick 이 한다. LLM 이 장중에 판단하는 구조는
  금지 (6-12 설계 원칙).
- **buy-only**: 자동 매도 없음. 매도는 앱/텔레그램 수동. 매도 자동화 제안 금지.
- **독립 경로**: slow/fast loop·tracker·position_sheets·executions/outcomes 를 건드리지
  않는다. 자체 `dca_campaigns`/`dca_slice_executions`(migration 026) 만 쓴다. 그래서
  paper 알파 측정에서 구조적으로 제외된다 — 측정 파이프라인이 보는 테이블에 안 들어간다.

## 구조

- `state.py` — enum + Campaign/SliceExecution dataclass. `cumulative_filled_krw` 가 cap
  불변식의 단조증가 진실 공급원. 시세/잔고에서 역산 금지.
- `planning.py` — 순수 함수(예산·가속·슬롯). 모든 cap 클램프가 `compute_slice_budget`
  한 곳을 지난다. PG 없이 단위 테스트.
- `repository.py` — asyncpg. 멱등의 핵심은 `acquire_slice` 의 ON CONFLICT(slice_key)
  DO NOTHING RETURNING. `finalize_slice` 가 슬라이스 terminal + 누적 가산을 한 트랜잭션으로.
- `executor.py` — 한 슬라이스 집행(cap강제·시장가·부분체결 잔량취소·VI 5분재시도·crash복구).
  부분체결 처리는 fast_loop/entry_executor.py(2026-05-21 사고 학습) 패턴.
- `tick.py` — 가드 우선순위 상태기계(G0 cap → G1 VI재시도 → G2 오늘처리됨 → G3 가속 →
  G4 예정 집행). 절대 예외를 밖으로 던지지 않는다(scheduler 재호출 = 이중집행 위험).

## 불변식 (깨면 안 됨)

- `cumulative_filled_krw ≤ cap_krw` 항상. 모든 예산이 `cap - cumulative` 로 클램프된다.
- 가속은 "다음 예정 슬라이스 당겨오기"일 뿐 추가 매수가 아니다 — 총량 불변, 타이밍만 변경.
- 같은 날 가속과 예정 집행 이중집행 금지 — `today_settled` + `slices_done` 두 겹으로 막는다.
- tick 핸들러는 예외를 내부에서 삼킨다.

## 구동

job-worker(apscheduler) 핸들러 `user_directed_dca_tick`, cron `* 9-15 * * 1-5`(매 분).
활성 캠페인 없으면 즉시 no-op. 휴장일 가드 + 글로벌 STOP/PAUSE 존중. 텔레그램 명령은
`telegram_bot/dca_command.py` + handler `/dca`.

## 검증 사다리 (본자금 전, 순서 고정)

dryrun(실주문 없이 로직) → smoke(저가 종목 소액 실거래) → production(본자금). 안전요건
6개(누적 cap·멱등성·confirm echo·persist·VI 페일세이프·cost cap)는 구현 편의로 생략 금지.
