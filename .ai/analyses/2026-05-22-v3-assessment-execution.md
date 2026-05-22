# v3 Assessment — 매매 실행·청산 도메인 (execution)

작성: 2026-05-22 / 에이전트: execution / Step 2 (v3 냉정 평가)
입력: `.ai/analyses/2026-05-22-v2-teardown-execution.md` §4 — v3 비교 훅 15항
대상: v3 = prime-jennie-runtime repo (HEAD e708586) + MS-01 라이브.
방법: §4 훅을 라이브 v3 코드(file:line)·데이터에 1:1 대조 → KEPT / LOST / IMPROVED / NEW-DEFECT.

**정정 베이스라인 (Step 1 결론):** v2-native는 2주·소표본·미검증. 본 문서는 "v3가 v2 성과보다 낫다/못하다"를 판정하지 않는다. 비교축은 **(a) 설계 차원** **(b) v3 라이브 데이터** 둘뿐이다.

v3 실행 도메인 구조: 느린 루프(`slow_loop`)가 종목별 `PositionSheet`(entry/exit/size 규칙 전부 포함)를 발행 → `fast_loop` 단일 프로세스가 소비·실행. v2의 4개 서비스(buy-scanner / buy-executor / sell-executor / price-monitor)가 `fast_loop` 하나로 통합됨.

---

## 1. 훅별 판정표

| # | 훅 (v2 설계) | 판정 | 핵심 증거 |
|---|---|---|---|
| 1 | 다층 청산 규칙 우선순위 정렬·순수함수 분리 | **KEPT (축소)** | 9종 rule, `exit.rules` 배열 first_match 순회(`exit_evaluator.py:71-84`), 각 rule이 순수 `_check_*` 함수. 단 v2 12종→v3 9종 — hard_stop·profit_lock·atr_stop 제거 (→§2) |
| 2 | 트레일링 국면별 drop% + MACD/death cross 조기발동 | **LOST** | `_check_trailing_tp`(`exit_evaluator.py:187-211`)는 고정 `activate_pct`/`drop_pct`만. regime 분기·MACD 가중 없음. 값은 strategy_tag별 정적(`scout/prompts.py:262-263`) |
| 3 | Profit Lock ATR 동적 trigger (L1/L2) | **LOST** | v3에 profit_lock rule 자체가 없음. `profit_floor`는 고정 %(=v2 profit_floor), ATR 동적 trigger 아님 |
| 4 | Breakeven / Profit Floor 단계별 보호막 | **KEPT/IMPROVED** | breakeven·profit_floor 둘 다 존재. breakeven은 청산 대신 **실제 SL 주문을 상향**(`exit_evaluator.py:242-250` `should_close=False`+`new_sl_price`) — v2는 단순 체크만 |
| 5 | 시간기반 청산 — 보유일수 채우나 / holding_days=0 버그 고쳤나 | **NEW-DEFECT (v2 버그 재현)** | tick_loop이 `evaluate_exit(sheet,state,tick)`만 호출(`tick_loop.py:203`) → `entered_business_days` 기본값 0 → hold_days time_stop 영구 미발동. ↓상세 §4.1 |
| 6 | high watermark 저장·재시작 생존·TTL | **KEPT/IMPROVED** | `position_state:{sheet_id}` 단일 JSON에 high_watermark 포함, 부팅 시 `load_from_redis()` 복원(`position_tracker.py:92-117`). TTL 없음(close 시 명시 삭제) — v2의 30일 TTL 만료 리스크 제거. 라이브 검증됨(KODEX200) |
| 7 | 틱의 당일 고가(high) 반영 | **KEPT (v2 갭 그대로)** | streamer가 `high`를 STREAM_PRICES에 발행(`streamer.py:314-323`)하나 `_parse_tick`이 무시(`tick_loop.py:453-461`), TickData에 high 필드 없음. high_watermark는 `tick.price`만으로 구성 — v2와 동일 결함 |
| 8 | 평단가 SSOT | **CHANGED** | v3 entry_price = confirm_order의 실체결 avg_price를 PositionState에 기록(`entry_executor.py:229`). v2는 KIS 잔고 평단 직접 사용. v3는 자기 기록값 — `positions_reconcile` job이 KIS와 교차검증 |
| 9 | 진입 전략·게이트 수, 차등 우회 | **CHANGED (아키텍처)** | v2의 9전략×13게이트×차등우회 → v3는 slow_loop 게이팅(macro gate·중복·cooldown) + sheet별 entry conditions 6종(`schema.py:132-184`) + fast_loop dedup. 패턴탐지가 아닌 조건충족 모델 |
| 10 | 부분체결 잔량 취소 (entry/sell 양쪽) | **IMPROVED (entry) / NEW-DEFECT (exit)** | entry: 부분체결 시 잔량 취소+terminal 재조회(`entry_executor.py:197-219`, e708586) — v2 under-count 버그 fix. exit: 부분체결 시 잔량 미취소 (↓§4.3) |
| 11 | ATR risk-parity 사이징 | **LOST** | v3 sizing = `total_asset × final_pct × intraday_mult` 캡(`app.py:120-149`). final_pct = base×macro×risk. ATR·1R·portfolio-heat 없음 — 고정비율 모델로 대체 |
| 12 | 진입/청산 분리 프로세스 + 독립 consumer group | **LOST (트레이드오프)** | v3 fast_loop = 단일 프로세스·단일 이벤트루프·단일 tick consumer group `fast_loop_ticks`. v2의 scanner-group/monitor-group 2분리 → 1통합. ↓§4.4. 대가로 통합 상태모델 획득 |
| 13 | KIS 단일 통제점·rate limit·circuit breaker·WS↔폴링 폴백 | **KEPT/IMPROVED** | gateway 중앙화 유지(`server.py`, 전 호출 limiter+breaker 경유). rate limiter는 token bucket으로 **개선**(v2 sliding-window burst 버그 EGW00201 fix, `rate_limiter.py:1-11`). circuit breaker 동등 async 포팅. 폴링 폴백 유지 |
| 14 | emergency stop / pause / dryrun 글로벌 플래그 | **KEPT** | `control.state:*` (SystemState)로 재키잉. entry 차단(`app.py:122`), exit도 STOP 시 차단(`exit_executor.py:167-181`), dryrun 양 executor 처리. v2 `trading_flags:*`는 legacy |
| 15 | 청산 트리거 실시간 틱 vs 폴링/배치 | **KEPT** | exit 평가는 STREAM_PRICES 매 tick(`tick_loop.py:191-203`) — 실시간 유지 |

---

## 2. 회귀 (LOST)

### 2.1 청산 규칙의 적응성 상실 — 가장 큰 설계 회귀
v2 §2.2의 핵심 강점("변동성·국면·기술경고 3축으로 변조되는 동적 트레일링")이 v3에서 **정적 파라미터로 평탄화**됐다:
- **트레일링**: v3 `trailing_tp`는 `activate_pct`/`drop_pct` 고정 2값. 시장 국면(강세/약세)에 따른 drop% 차등 없음, MACD·death cross 감지 시 조기발동 없음. 값은 strategy_tag별로 scout 프롬프트에 박힌 정적값(GAP_UP_REBOUND drop 0.03 / SECTOR_MOMENTUM drop 0.03).
- **Profit Lock 소멸**: v2의 ATR 비례 동적 trigger(L1/L2)가 v3엔 아예 없다. v3 `profit_floor`는 v2의 *profit_floor*(고정 %)에 대응할 뿐, *profit_lock*이 아니다.
- **ATR 역할 소거**: v3 청산에 ATR 기반 rule(atr_stop)이 없고, 사이징도 ATR risk-parity가 아니다. v3 실행 도메인에서 ATR은 사실상 미사용.

설계 관점 평가: 이것이 의도된 단순화일 수 있다(v3는 "결정론 + sheet 계약" 지향). 적응성을 런타임(monitor가 매 tick regime 조회)에서 **생성시점**(strategy engine이 sheet에 값을 굽는 것)으로 옮길 수도 있었으나, `strategy_policy.yaml`의 `default_exit_rules`는 strategy_tag별 **평면 정적값**일 뿐 regime 분기가 없다(trailing drop_pct = tag별 0.03/0.03/0.04 고정). strategy engine도 macro regime을 `size`(macro_multiplier)에만 반영하고 exit rule엔 안 쓴다(`engine.py:443-509`). 따라서 적응성은 이전된 게 아니라 **소거**됐다.

### 2.2 ATR risk-parity 사이징 → 고정비율
v2 사이징은 `1R=ATR×2`, 자산 1% risk, portfolio-heat 5% 상한의 risk-parity. v3는 `total_asset × final_pct`(비율) + notional 캡. 종목 변동성이 수량에 안 들어간다. 변동성 큰 종목과 작은 종목이 같은 자산비율로 잡힌다.

### 2.3 청산 규칙 수 12→9
제거: hard_stop(-10% gap-down 안전망), profit_lock(L1/L2), atr_stop. fixed_sl(스키마 필수, ≤10% 캡)이 hard_stop·fixed_stop을 흡수하나, gap-down 별도 안전망과 ATR 기반 손절은 사라졌다. time-tightening(보유기간 경과 시 손절선 조임)도 없다.

---

## 3. 진짜 개선 (IMPROVED)

이 항목들은 v2의 **실제 결함을 고친** 것 — 미화 아님, 코드·근거 확인됨.

1. **Entry 부분체결 under-count 버그 fix** (e708586). v2 teardown §3.3에서 지목한 "매수 부분체결 잔량 미취소 → KIS 실보유 > 기록" 버그를 v3가 `entry_executor.py:197-219`에서 정확히 수정 — 부분체결 감지 시 잔량 취소 + terminal 재조회로 실체결량 확정. (단 exit 경로엔 미적용 — §4.3.)
2. **Rate limiter sliding-window → token bucket** (`rate_limiter.py`). v2의 윈도우 경계 burst(200ms에 10건)가 KIS `EGW00201`을 트립한 2026-05-13 사고를 token bucket으로 구조적 차단. v2 결함의 직접 수정.
3. **Breakeven이 실제 SL 주문을 상향**. v2 breakeven은 평가 시 체크만 했으나, v3는 `should_close=False`+`new_sl_price`로 KIS SL 주문 자체를 이동(`exit_evaluator.py:242-250`). 틱 누락 시에도 SL이 브로커 측에 박혀 있어 더 견고.
4. **통합 상태모델 + 재시작 복원**. v2는 종목별로 흩어진 Redis 키(`watermark:`·`scale_out:`…). v3는 `position_state:{sheet_id}` 단일 JSON, 부팅 시 `load_from_redis()` 일괄 복원. TTL 만료 리스크 없음. 같은 종목 복수 sheet도 깔끔히 분리.
5. **연속 매도실패 abandon 가드** (`exit_executor.py:83-131`, `max_exit_failures=3`). 외부 청산으로 KIS 잔고 0이 된 stale sheet가 매 tick 무한 매도 시도 → KIS rate limit 폭주하는 사고(2026-05-13)를 차단. v2엔 없던 보호.
6. **부팅 시 state↔KIS 잔고 mismatch 검사** (`check_state_kis_mismatch`, `app.py:222`) + `positions_reconcile` job. v2엔 없던 drift 조기검출.
7. **per-sheet 예외 격리**. tick_loop이 sheet 하나의 exit/entry 실패를 try/except로 격리(`tick_loop.py:239-247`) — 한 sheet 실패로 루프 전체가 죽는 사고(5-13) 학습 반영.

---

## 4. 새 결함 — 통합 이음매 (NEW-DEFECT)

### 4.1 ★ time_stop(hold_days)이 죽어 있다 — v2 최악 결함의 재현
v2 teardown §3.1은 v2의 최악 결함으로 "시간기반 청산이 `bought_at` 미설정 → `holding_days`=0 → Time Exit 영구 미발동"을 지목했다. **v3는 이 결함을 새 형태로 그대로 재현했다.**

- `exit_evaluator.evaluate()`는 `entered_business_days` 파라미터를 받고, `_check_time_stop`은 hold_days 모드에서 `entered_business_days >= rule.value`를 본다(`exit_evaluator.py:280-313`).
- 그러나 유일한 라이브 호출부 `tick_loop.py:203`은 `evaluate_exit(sheet, state, tick)` — **3개 인자만 전달**. `entered_business_days`는 기본값 **0**으로 고정. grep 확인: `entered_business_days`를 채워 호출하는 코드는 repo 전체에 0건.
- 결과: hold_days 모드 time_stop은 `0 >= N`(N≥1)이 영구 거짓 → **절대 발동 안 함**.
- **라이브 데이터**: position_sheets 1355건 중 `time_stop` 모드 분포 = **hold_days 1307건(96.5%) / eod 48건(3.5%)**. scout 프롬프트가 4개 strategy_tag 전부 hold_days 예시를 줌(`scout/prompts.py:261-264`). executions 133 sell 중 time_stop 청산 **0건**. fast-loop 로그 720시간 내 `time_stop` 언급 **0건**.
- v2 대비 악화 요인: v2는 `bought_at`이 KIS API에서 안 와서 데이터 자체가 없었다. **v3는 `PositionState.entered_at`을 자기 객체에 갖고 있다**(`domain.py:25`) — 영업일수 계산 데이터가 손 안에 있는데 tick_loop이 안 쓴다. 순수 배선 누락.
- 안전망: `eod` 모드(48 sheet)는 `entered_business_days` 불필요라 정상 작동. 그러나 96.5% sheet는 시간 청산 안전망 없음.

### 4.2 death_cross rule도 죽어 있다
같은 이음매. `_check_death_cross`는 `daily_closes`가 없으면 즉시 None(`exit_evaluator.py:372`). tick_loop은 `daily_closes`를 전달하지 않음(기본 None). grep: 채워 호출하는 코드 0건.
- **라이브**: position_sheets 1355건 중 **652건(48%)이 death_cross rule 보유** — 전부 dead rule. executions 133 sell 중 death_cross 청산 0건, 로그 0건.
- v2는 monitor가 일봉 60개를 fetch해 death_cross를 실제 계산했다(v2 teardown §1.4). v3는 그 데이터 공급선을 끊었다.

### 4.3 Exit 부분체결 잔량 미취소 — e708586 fix가 entry에만 적용
`entry_executor`는 부분체결 잔량을 취소하지만(§3.1), `exit_executor.execute()`는 안 한다. 전량청산 결정(`portion>=1.0`)에서 시장가 매도가 부분체결되면 `fully_closed = state.quantity<=0 or decision.portion>=1.0` → `portion>=1.0`이라 **True** → `tracker.close()`로 sheet 종료(`exit_executor.py:266-274`). 미체결 잔량 주문은 KIS에 살아남아 추적 밖에서 나중에 체결 → sheet는 닫혔는데 KIS는 잔주 보유. e708586이 진단한 under-count의 거울상(매도 측). 시장가 매도는 보통 즉시 전량체결이라 빈도는 낮으나, 수정 비대칭은 명백한 갭.

### 4.4 단일 장애도메인 — 4서비스→1프로세스 통합
v2는 scanner·monitor가 별도 프로세스, `kis:prices`를 독립 consumer group 2개로 소비 — monitor가 죽어도 scanner 생존. v3 `fast_loop`은 단일 프로세스·단일 이벤트루프, tick consumer group 하나(`fast_loop_ticks`)가 exit+entry재평가+forced-liq+분봉집계를 모두 수행.
- `app.py:323-333`: 5개 task를 `asyncio.wait(FIRST_COMPLETED)` — 하나라도 끝나면 전체 종료→컨테이너 재시작. entry/exit/control이 한 운명.
- `tick_loop.run()`은 redis `ConnectionError` 시 `break`(`tick_loop.py:121-123`) → 일시적 redis 끊김에도 fast_loop 전체 재시작. v2 consumer는 `sleep(5);continue`로 자체 재시도했다 — v3가 이 지점은 **덜 견고**.
- 완화책 존재: per-sheet 예외 격리(§3.7), exit-fail abandon(§3.5), 재시작 시 state 복원. 그리고 통합의 대가로 **단일 일관 상태모델**(PositionTracker = SSOT)을 얻었다 — v2의 scanner/monitor가 각자 KIS를 조회해 drift나던 문제 제거.
- 평가: 순수 회귀가 아니라 **트레이드오프**. 단 "장애도메인 분리"라는 v2 강점은 상실했고, redis 재연결 미흡은 독립적 약점.

### 4.5 6/9 청산 rule이 라이브에서 미발동
executions 133 sell의 exit_reason 분포: **fixed_sl 90 / trailing_tp 29 / breakeven 13 / (null) 1**. 9종 중 실제 발동 확인 = 3종뿐. time_stop·death_cross는 §4.1·4.2로 죽었고, profit_floor·scale_out·fixed_tp·overextension_exit은 측정 구간(5-06~5-18) 내 미발동. v2 teardown이 "12종 광고, 라이브 10.5종"이라 했는데, v3는 "9종 정의, 라이브 실동작 확인 3종 + 죽은 2종 + 미관측 4종". 표본이 작아 "미관측 4종"을 dead로 단정할 순 없으나, time_stop/death_cross 2종은 배선상 확정 dead.

---

## 5. Step 3 보완 후보 (우선순위·규모 — elaborate 설계 금지)

| 우선 | 항목 | 규모 | 근거 |
|---|---|---|---|
| **P0** | `tick_loop._evaluate_exits`가 `evaluate_exit` 호출 시 `entered_business_days` 전달 — `state.entered_at`→now 영업일수 계산 | 小 (~10줄, 데이터 이미 보유) | §4.1. 96.5% sheet의 time_stop이 죽어 있음. v2 최악 결함 재현분 |
| **P0** | 같은 호출에 `daily_closes` 전달 — 종목별 일봉 캐시(v2 monitor `_compute_all_indicators` 패턴 존재) | 中 (~40-60줄, fetch+캐시) | §4.2. 48% sheet의 death_cross가 죽어 있음 |
| P1 | `exit_executor`에 부분체결 잔량 취소 적용 (entry_executor 미러) | 小 (~10줄) | §4.3. e708586 fix의 비대칭 해소 |
| P1 | `tick_loop.run()` redis `ConnectionError` 시 `break` → 재시도 루프 | 小 (~5줄) | §4.4. 일시적 redis 끊김에 전체 재시작 회피 |
| P2 | 트레일링 `drop_pct`를 strategy engine이 macro regime별로 차등 부여 (생성시점 적응성) | 中 | §2.1. 단 "정적 단순화 유지"도 유효 선택 — 설계 결정 필요. 측정 후 판단 권장 |
| P2 | (관찰) profit_floor·scale_out·fixed_tp·overextension 실발동 여부 — 표본 누적 후 재측정 | — | §4.5. 현재 미관측이 dead인지 기회부재인지 불명 |

ATR risk-parity 사이징 복원(§2.2)·profit_lock 부활(§2.3)은 후보에서 제외 — v3의 "결정론 sheet 계약" 지향과 상충하는 큰 설계 변경이라, Step 3 보완이 아니라 별도 설계 안건. 본 평가의 권고는 **죽은 배선부터 잇는 것**(P0)이다 — 9종 rule을 광고하면서 2종이 확정 dead인 상태가 가장 시급하다.

---

### 못 알아낸 것 (정직 고지)
- v3 라이브 청산 표본(133 sell, 2026-05-06~05-18)은 STOP으로 5-18 이후 끊겨 작다. profit_floor 등 4종 "미관측"을 dead로 단정 못 함.
- entry 경로 `account_sizer`의 `intraday_mult`(risk_throttle) 실측값·발동 이력 미확인 — sizing 정확도 영향 범위 미평가.
- 실제 발행 sheet의 exit rule은 scout `exit_hint`가 있으면 그것을, 없으면 policy default를 쓴다(`engine.py:501-507`). 본 분석은 policy default(yaml) + 발행된 sheet의 집계 DB값을 봤고, scout hint가 default를 얼마나 자주 덮는지의 비율은 미집계.
