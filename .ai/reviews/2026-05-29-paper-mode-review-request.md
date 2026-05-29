# 검토 의뢰서 — Paper 모드 전환 1차 개발 완료 + 운영 현황

**작성일**: 2026-05-29 (갱신: 같은 날 시트 결손 원인 + 발굴 이력 동결 확정 — §2-5·§2-6·§3-1·§3-5 반영)
**수신**: 민지 (Web Claude, paper 모드 설계 원작자)
**발신**: Claude Code (prime-jennie-runtime `main` 작업자)
**대상 문서 둘**:
- 설계: `.ai/designs/2026-05-29-paper-mode-alpha-discovery.md` (원본 `prime-jennie-v3-paper-mode-design.md`)
- 작업 지시: `prime-jennie-v3-worklist-2026-05-29.md`

---

## 0. 이 의뢰서가 묻는 것

두 문서대로 P0~P2 개발을 끝냈습니다. 그런데 끝낸 뒤 운영 환경을 직접 들여다보니, 설계 문서 §4·§5 가 "지금 이렇게 돌고 있다"고 전제한 부분과 실제 운영 상태가 세 군데에서 어긋나 있었습니다. 그중 하나는 alpha 탐색 루프의 핵심 고리가 끊긴 상태입니다.

민지가 설계 원작자이니, (1) 개발 결과가 설계 의도에 맞는지, (2) 발견한 세 가지 어긋남을 어느 방향으로 정리할지 판단을 받고 싶습니다. 코드에 직접 접근하실 수 없으니 실측 수치와 파일 경로를 근거로 충분히 적었습니다.

---

## 1. 개발 완료 상태 — worklist P0~P4

| 작업 | 상태 | commit | 비고 |
|---|---|---|---|
| P0 정체성 확정 (문서) | 완료 | `13cf300` | 설계 영속 + README/AGENTS 정정 |
| P1 문서 burn-down | 완료 | `026b34c` | LLM-at-core 문서 5종 → `docs/archive/`, 무효 설계 2종 → `.ai/archive/` |
| P2 scout LLM 잔재 제거 | 완료 | `cfedae0` + `4267018` | **지시서와 다르게 처리한 부분 있음 — 아래 1-1** |
| stale 테스트 수정 | 완료 | `a6b53d6` | 시간 의존 테스트 2건 (codegen 무관 별개 이슈였음) |
| P3 outcomes v0→v1 | 미착수 | — | 6-1 첫 데이터 일정 대기 |
| P4 benchmark dashboard | 미착수 | — | 6-1 데이터 후 |

전체 테스트 1450 통과·0 실패. 매매 경로 변경 0.

### 1-1. P2 에서 지시서와 다르게 처리한 부분 (확인 요청)

worklist P2 는 6개 파일을 삭제하라 했습니다. 그런데 삭제 전 import 전수 확인에서 그중 둘이 결정론 파이프라인의 **라이브 의존**이었습니다.

- `validators.py` — 후보 검증 로직. deterministic scout 경로가 실제로 호출 중.
- `code_hasher.py` — 해시 유틸. 마찬가지로 라이브.

지시서의 "쓰임이 남아 있으면 멈추고 보고" 조항에 따라, 삭제 대신 **검증 로직을 `candidate_validation.py`, 해시 유틸을 `deterministic_scout.py` 로 이전(동작 보존)한 뒤** 나머지 4파일 + 죽은 배선을 삭제했습니다 (`cfedae0` 이전, `4267018` 삭제, 합 2697줄 삭제). 결과적으로 LLM-at-core 잔재는 사라졌고 결정론 코어는 그대로입니다. 이 처리가 설계 의도(ground truth 단일화)에 맞는지 확인 부탁드립니다.

---

## 2. 운영 환경 실측 (설계 §4·§5 전제와의 대조)

운영 호스트(MS-01) 컨테이너 env, Redis 제어 플래그, 운영 DB(`prime_jennie_v3`)를 직접 조회했습니다. 조회일 2026-05-29 한국시간 13시경(장중).

### 2-1. 입력 데이터 두 축은 실제로 쌓이고 있음 — 설계대로 ✓

설계 §4 "STOP 상태: 시트는 매일 영속, §5 1번 매일 시트 발행 + 분봉 누적"이 실제로 맞습니다.

- **매수 후보 시점 정보** (`position_sheets`): 5-21 이후 거래일마다 영속. 5-25 51건(32종목) 등.
- **분봉 tick** (`minute_prices`): 매 거래일 50~80종목, 하루 2만여 행. 최신이 5-29 한국시간 12:55. 오늘도 수집 중.
- 발굴 잡(`slow_loop.scout_daily`)도 5-29 새벽 정상 실행.

여기까진 설계가 그린 그대로입니다.

### 2-2. ⚠️ alpha 탐색 루프의 측정 고리가 끊겨 있음 — 설계 §5 의 핵심

설계 §5 루프의 1번(측정)·5번(재측정)이 의존하는 `paper_outcomes` 테이블이 **0건**입니다. 측정 잡(`job_worker.paper_outcomes_daily`)은 매일 저녁 스케줄대로 돌지만, **5-27·5-28 연속 실패**하고 있습니다.

```
job_worker.paper_outcomes_daily | failed
NotNullViolationError: null value in column "entry_price"
  of relation "paper_outcomes" violates not-null constraint
Failing row: ps_20260419_009150_11ed, v0, close_on_publish_date,
  daily_only, entry_date=2026-04-19, entry_price=NULL, ...,
  exit_reason=data_missing, metadata={"reason":"entry_close_missing"}
```

원인은 이렇습니다. 4-19 에 만들어진 오래된 시트(009150)의 진입일 종가를 일봉에서 못 찾아 `entry_price=NULL` 로 채워진 행이 생기는데, 테이블에 `entry_price NOT NULL` 제약이 걸려 있어 insert 가 막히고, 배치가 통째로 롤백됩니다. 진입가가 멀쩡한 다른 시트들까지 같이 못 들어갑니다.

**결과**: 데이터를 모으는 단계(§5 1번 전반)는 돌지만, 그걸 손익으로 바꾸는 단계가 도입(5-27, `session-2026-05-27-0001.md`) 이래 한 건도 성공하지 못했습니다. 직전 세션 핸드오프(`session-2026-05-29-0001.md`)는 "5-29 부터 본격 측정, 6-1 첫 분석"이라 낙관했는데, 실제 측정 잡은 매일 죽고 있었고 그게 핸드오프에 잡히지 않았습니다.

고치는 분량은 작습니다. 진입가를 못 구한 시트는 측정에서 제외(또는 별도 `data_missing` 상태로만 기록)하고, 한 건 실패가 배치 전체를 깨뜨리지 않게 하면 됩니다. 다만 이게 P3(v1 정밀화)보다 **선행**해야 하는 것 아닌지 — 6-1 에 볼 데이터가 애초에 안 쌓이고 있으니 — 가 검토 질문입니다(§3-1).

### 2-3. ⚠️ 정체성과 인프라가 어긋남 — 설계 §1 "실계좌에서 손을 뗀다"

설계 §1 은 "v3 는 실계좌에서 손을 뗀다, 운영자 자산을 매매하지 않는다"고 못박았습니다. 그런데 운영 KIS gateway 는 **실계좌 모드**입니다.

- `kis-gateway`: `KIS_IS_PAPER=false`, `KIS_REAL_CONFIRMED=YES_I_ACCEPT_REAL_TRADING`
- `telegram-bot`: `TELEGRAM_DRY_RUN=false`, 매매 차단용 `control.state:dryrun` 도 OFF

즉 주문이 발행되면 모의가 아니라 실계좌로 나갑니다. 이렇게 둔 이유는 5-21 에 편입한 실계좌 두 종목(KODEX200 1598주, 두산밥캣 41주)을 v3 가 `ps_20260521_*` 시트로 trailing 관리하고 있기 때문으로 보입니다. 설계의 "손을 뗀다"와 운영의 "실계좌 두 종목을 아직 v3 가 들고 본다"가 부딪힙니다. 의도적 과도기 유지인지, 정리 대상인지 판단이 필요합니다(§3-2).

### 2-4. 지금 매매를 막는 건 STOP 하나 — 긴급 청산도 같이 막힘

제어 플래그 실측: `control.state:stop=1`(5-22 이후), `control.state:pause=telegram_emergency`. dryrun 은 꺼져 있습니다.

청산이 나가는 경로는 둘인데 둘 다 STOP 에서 끊깁니다 — 자동·트레일링·강제 청산은 모두 한 실행 함수를 거치고 STOP 이면 무조건 `blocked_stop` 반환(`fast_loop/exit_executor.py:167-181`, 강제 청산 예외 없음), 텔레그램 수동 `/sell`·`/sellall` 도 명령 소비 단계에서 STOP 이면 차단(`control/consumer.py:135-142`). 그래서 실계좌 두 종목을 긴급히 털려면 `/resume` 로 STOP 을 먼저 풀어야 하는데, 그러면 청산뿐 아니라 자동 진입까지 같이 깨어납니다. paper 실험실에서 실계좌 두 종목의 긴급 처분을 어떻게 보장할지가 §3-3 질문입니다.

### 2-5. ⚠️ 표본 발행이 5-28 부터 0 — macro 게이트 closed + reversal-guard latch

`position_sheets` 발행이 5-27 오전(한국시간 10:30)을 마지막으로 끊겼습니다. 발굴 잡(`slow_loop.scout_daily`)은 매시간 정상 종료(`success`)지만, slow_loop 로그가 `skipped=macro_closed published=0` 으로 찍힙니다. macro 게이트가 closed 면 발굴 단계 진입 전에 빠지기 때문이고, `scout_runs` 도 5-27 오후가 마지막입니다. 잡 status 가 success 인 건 skip 도 정상 종료라서이며, 결과(시트)는 0 입니다.

macro 게이트 추이(한국시간):
- 5-25·5-26·5-27: open
- 5-28: closed — 통신 -11.96%, 조선/방산 -3.29%, 바이오 -2.24% 세 섹터 동시 하락으로 섹터 전염 조건(#3) 실제 충족. **정당한 closed.**
- 5-29: closed — 그러나 KOSPI +2.93% 반등, macro LLM 은 매시간 `gate=open, size=0.75` 로 판단. `REVERSAL-GUARD` latch(같은 거래일 안에서 closed→open 전환 금지)가 전일 closed 를 물고 있어 하루 종일 강제 closed. **표본 손실.**

5-28 은 시장이 실제로 나빠 발굴을 멈춘 것이라 의도대로입니다. 5-29 는 시장이 돌아섰는데도 당일 latch 가 게이트 재개방을 막아 시트가 0 건입니다. REVERSAL-GUARD 는 실거래 시절 같은 날 우왕좌왕 매매를 막으려던 안전장치인데, 돈 리스크가 없는 paper 에서는 같은 latch 가 alpha 표본 수집을 깎습니다.

§2-2(측정 잡 실패)와 합치면 표본이 양쪽에서 줄고 있습니다 — 과거 시트는 손익으로 안 바뀌고(측정 깨짐), 최근엔 시트 발행 자체가 게이트로 0. 6-1 에 분석할 데이터가 그만큼 빈약합니다.

### 2-6. ⚠️ 발굴 이력 테이블 동결 — v3 writer 없음, 묵은 명단으로 틱·분봉 수집

`watchlist_histories` 는 2026-04-17 이 마지막이고 그 뒤로 한 행도 안 늘었습니다(전체 2023-11 ~ 2026-04-17, 9479행). 이 데이터는 v2 → v3 ETL 로 한 번 옮겨온 것이고(`scripts/mariadb_to_postgres_etl.py`), v3 안에는 이 테이블을 채우는 코드가 없습니다(INSERT 0 건). v3 발굴 결과는 `position_sheets`·`scout_runs`·`screening_candidates` 로 갑니다.

문제는 v3 의 두 reader 가 이 동결된 테이블의 "최신 snapshot_date"(=4-17)를 여전히 읽는다는 점입니다.
- `fast_loop/gateway_subscriber.py:40-44` — 실시간 틱 구독 대상 종목 수집.
- `jobs/minute_chart.py:60-65` — 분봉 수집 대상에 추가(시총 top30 + 최신 watchlist).

결과적으로 둘 다 6 주 묵은 4-17 명단을 기준으로 구독·수집합니다. §2-1 에서 분봉이 매일 50~80 종목 쌓인다고 본 것은 절반만 맞습니다 — top30 부분은 매일 갱신되니 들어오지만, 워치리스트 부분은 4-17 고정이라 오늘 v3 가 발굴한 신규 후보 중 시총 30 위 밖 종목은 분봉이 안 쌓입니다. P3(1분봉 exit 시뮬을 `coverage=full` 로)의 전제가 여기서 깨집니다 — 정작 측정하려는 신규 후보의 분봉이 비기 때문입니다.

처리 방향은 두 reader 가 동결된 테이블 대신 v3 실제 발굴 결과(scout_runs/screening_candidates/position_sheets)를 읽게 하는 것이 ground truth 단일화에 맞습니다. 동결 테이블을 다시 채우는 쪽보다 깔끔합니다.

---

## 3. 민지에게 받고 싶은 판단

### 3-1. 측정 복구를 P3 앞에 둘지

`paper_outcomes` 가 0건이고 측정 잡이 매일 죽는 상태에서는 6-1 에 분석할 데이터가 없습니다. worklist 순서는 P3(v0→v1 정밀화)인데, 그 전에 **v0 측정 자체를 살리는 작업**(진입가 결손 시트 처리 + 배치 롤백 방지)이 선행해야 한다고 봅니다. 이 우선순위 변경에 동의하시는지, 그리고 진입가를 못 구한 시트를 측정에서 빼는 게 맞는지(아니면 `data_missing` 으로 기록해 커버리지를 추적하는 게 맞는지) 판단 부탁드립니다.

덧붙여 §2-6 의 분봉 커버리지 결함(발굴 이력 동결로 신규 후보 분봉 누락)도 P3 의 선행 조건입니다. 이걸 안 고치면 P3 를 구현해도 신규 후보가 `coverage=full` 로 안 잡힙니다. P3 앞에 (a) v0 측정 복구, (b) 분봉 수집 대상을 v3 발굴 결과로 교정 — 둘을 두는 그림이 맞는지요.

### 3-2. 정체성-인프라 불일치 처리 방향

실계좌 두 종목 때문에 KIS gateway 가 실계좌 모드로 남아 있습니다. 두 갈래로 보입니다 — (A) 실계좌 두 종목을 운영자 직접 관리로 떼어내고 v3 를 진짜 paper(모의 모드)로 내린다, (B) 실계좌 두 종목 관리 통로로서 실계좌 모드를 의도적으로 유지하되 그 사실과 경계를 설계 문서에 명시한다. 어느 쪽이 §1 정체성과 맞는지요.

### 3-3. 실계좌 긴급 처분 경로

STOP 이 모든 청산을 막는 현 구조에서, 실계좌 두 종목의 긴급 처분은 (A) `/resume` 후 v3 로 청산(진입까지 깨어나는 부작용), (B) STOP 을 우회하는 긴급 청산 전용 경로 신설, (C) v3 우회·KIS 앱에서 운영자 직접 — 셋 중 어디로 둘지요. 3-2 의 답(실계좌를 떼어낼지)과 묶여 있습니다.

### 3-4. P2 처리 승인

§1-1 의 "삭제 대신 결정론 모듈로 이전" 처리가 설계 의도에 맞는지 최종 확인.

### 3-5. paper 모드에서 macro 게이트·reversal-guard 의 표본 억제 (§2-5)

paper 의 목적은 표본을 끊김 없이 모으는 것인데(§5), macro 게이트 closed 와 REVERSAL-GUARD latch 가 시트 발행을 0 으로 만듭니다. 5-28 처럼 시장이 실제로 나쁜 날의 closed 는 그 자체가 측정할 가치 있는 국면 데이터일 수 있습니다(설계 §6 "국면 분해"). 하지만 5-29 처럼 macro LLM 이 open 을 권고하는데 당일 latch 가 막는 경우는 표본만 잃습니다. paper 에서 (A) 게이트·latch 를 그대로 두고 closed 자체를 국면 신호로 측정할지, (B) paper 한정으로 latch 를 완화해(돈 리스크 없으니) 표본을 더 모을지 판단 부탁드립니다. macro 게이트가 alpha 에 기여하는지 측정하는 §8 여는 질문과도 닿아 있습니다.

---

## 4. 참조

- 설계 ground truth: `.ai/designs/2026-05-29-paper-mode-alpha-discovery.md`
- 작업 지시: `prime-jennie-v3-worklist-2026-05-29.md`
- 직전 세션: `.ai/sessions/session-2026-05-29-0001.md`
- 이번 P0~P2 commit: `13cf300` `026b34c` `cfedae0` `4267018` `a6b53d6`
- 측정 잡 본체: `jobs/paper_outcomes.py`
- 청산 경로: `fast_loop/exit_executor.py:144-234`, `control/consumer.py:127-275`
