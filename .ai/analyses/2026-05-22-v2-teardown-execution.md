# v2 Teardown — 매매 실행·청산 해부 (execution)

작성: 2026-05-22 / 에이전트: execution / 대상: prime-jennie (v2, 마지막 커밋 2026-04-21)

대상 서비스: `buy-scanner`, `buy-executor`, `sell-executor`, `price-monitor`, `kis-gateway`.
모든 결론은 v2 실제 코드 인용(file:line). v2 실거래 데이터(MariaDB `jennie_db.trade_logs`)도 직접 조회·검증 — §1.7 참조.
**중요:** v2 stream 아키텍처의 라이브는 2026-02-19 v1→v2 컷오버 후 약 2주뿐. trade_logs 대부분은 v1 거래이며, §1.7에서 era 분리해 귀속함.
(v3 postgres `executions` 테이블은 v3 sheet 모델 전용, v2 잔존분 없음 — 확인함.)

---

## 1. v2가 한 일 · 메커니즘 (file:line)

### 1.1 전체 데이터 흐름

```
KIS WebSocket(H0STCNT0) ─▶ Gateway streamer ─▶ Redis Stream  kis:prices
                                                   │
                        ┌──────────────────────────┴──────────────────────────┐
                  buy-scanner (XREADGROUP scanner-group)        price-monitor (XREADGROUP monitor-group)
                        │                                              │
              stream:buy-signals                              stream:sell-orders
                        │                                              │
                  buy-executor                                  sell-executor
                        │                                              │
                  KIS Gateway ◀────────── 모든 KIS 호출 중앙 프록시 ──────────▶ KIS Gateway
                        │                                              │
                  DB trade_logs/positions                       DB trade_logs/positions
```

핵심: **틱 1개의 가격 스트림(`kis:prices`)을 scanner·monitor가 독립 consumer group으로 동시 소비**. 진입 감시와 청산 감시가 같은 피드를 공유하되 서로 독립. (`scanner/app.py:570-572`, `monitor/app.py:64-68`)

### 1.2 진입 — buy-scanner

- **틱→1분봉 집계**: `BarEngine.update()` 가 틱을 1분 캔들로 누적하고 VWAP·거래량비율을 함께 계산 (`scanner/bar_engine.py:54-110`). 바가 **완성될 때만** 전략 감지 (`scanner/app.py:289-291`).
- **전략 9종** (`scanner/strategies.py`): GOLDEN_CROSS, MOMENTUM, MOMENTUM_CONTINUATION(Bull 전용), DIP_BUY, VOLUME_BREAKOUT, WATCHLIST_CONVICTION, ORB_BREAKOUT, GAP_UP_REBOUND (RSI_REBOUND은 deprecated). 우선순위 순차 감지 후 첫 매치 반환 (`strategies.py:440-492`).
- **국면(regime) 분기**: BULL/SIDEWAYS에서만 Golden Cross, BULL 전용 Momentum Continuation, DIP_BUY 조정폭이 국면별로 다름 (`strategies.py:260-263`).
- **추격매수 방지**: Momentum은 5봉 상승률이 `max_gain_pct`(7%) 초과 시 비활성 (`strategies.py:181-185`).
- **리스크 게이트 13종 fail-fast**: `run_all_gates()` — min_bars, no_trade_window(09:00-09:15), danger_zone(14:00-15:00), rsi_guard, macro_risk(Risk-Off≥2/VIX Crisis), combined_risk(거래량+VWAP이격 동시), cooldown, stoploss_cooldown, sell_cooldown, trade_tier, overextension(이격률 60일), micro_timing(Shooting Star/Bearish Engulfing) (`scanner/risk_gates.py:289-346`).
- **전략별 게이트 우회 차등**: CONVICTION_ENTRY는 게이트 전면 우회(쿨다운만), ORB는 부분 우회(RSI guard·combined_risk 스킵), GAP_UP_REBOUND도 부분 우회 (`scanner/app.py:302-383`). 전략 성격에 맞춘 의도적 설계.
- **모멘텀 확인봉**: 모멘텀 계열 시그널은 즉시 발행하지 않고 `_pending_momentum`에 보류 → 다음 봉에서 가격이 시그널가 이상 유지될 때만 발행 (`scanner/app.py:417-473`).
- **시그널 쿨다운**: 동일 종목 `signal_cooldown_seconds`(600초) 재발행 차단 (`risk_gates.py:149-165`).

### 1.3 진입 주문 — buy-executor

`process_signal()` 9단계 파이프라인 (`buyer/executor.py:109-180`):
0. 장중시간(09:00-15:30) 1. emergency stop 2. BLOCKED tier veto 3. hard floor score 4. 이미 보유 4-1. 손절 쿨다운 4-2. 매도 24h 쿨다운 5. 일 매수횟수(6회) 6. 포트폴리오 정원(10) 7. 분산 락(`lock:buy:` NX EX180) 8. 사이징 9. 주문.

- **포지션 사이징** — ATR 기반 risk-parity (`buyer/position_sizing.py:94-227`):
  - `1R = ATR×2.0`, `risk_amount = 총자산 × 1% × 섹터배율`, `qty = risk_amount / 1R`.
  - 상한 3중 클램프: 동적 max position %(LLM≥80 → 18%, 그 외 12%), 현금하한(총자산 10% 유지), MAX_QUANTITY.
  - **Smart Skip**: 현금이 목표수량의 50% 미만만 허용하면 매수 포기 (`position_sizing.py:155-164`).
  - 사후 배율: tier(hybrid score 기반 5단계 0.6~1.0x), risk_tag(CAUTION 0.7x/DISTRIBUTION_RISK 0.0x), stale, macro position_multiplier (`position_sizing.py:180-195`).
  - **Portfolio Heat**: 누적 리스크 5% 상한 (`position_sizing.py:42-44, 166-178`).
- **Portfolio Guard (Layer 2)** — 섹터 종목수, 섹터 금액비중, 종목 금액비중, 국면별 현금하한 4종 순차 (`buyer/portfolio_guard.py:195-229`). STRONG_BULL에서 일부 한도 완화.
- **상관관계 체크**: 후보-보유종목 60일 종가 상관 ≥0.85면 차단 (`buyer/executor.py:472-493`).
- **주문 방식**: 모멘텀 전략 → **지정가**(현재가×1.003, KRX 호가단위 정렬), `momentum_limit_timeout_sec`(10초) 대기 후 미체결이면 취소; 그 외 → **시장가** (`buyer/executor.py:333-413`, `_align_tick_size:520-547`).
- **체결 확인**: `confirm_order()` 폴링 5회×3초 (`infra/kis/client.py:92-128`).

### 1.4 청산 감시 — price-monitor + exit_rules (★ 핵심)

#### 포지션·고점·상태 추적
- `refresh_positions()` — 5분 주기로 `kis.get_positions()`(KIS 잔고 API)를 받아 인메모리 `_positions` 교체. 동시에 종목별 daily_prices 60일 1회 fetch → **RSI·ATR·death_cross·MACD 일괄 계산 후 캐시** (`monitor/app.py:115-151, 407-437`).
- `process_tick()` — 틱마다 인메모리 `current_price` 갱신 → 매도 규칙 평가 (`monitor/app.py:153-182`).
- **High watermark (최고가)**: `_evaluate_position()`에서 `price > hw`이면 갱신. 저장소는 **Redis 키 `watermark:{code}` (TTL 30일)**, 5분마다 DB `positions.high_watermark`로 일괄 동기화 (`monitor/app.py:213-217, 464-501`).
- **평단가(buy_price)**: 별도 추적 안 함 — 매 평가 시 `pos.average_buy_price`(KIS 잔고 API의 `pchs_avg_pric`)를 그대로 사용. 즉 평단가의 single source of truth는 **증권사 잔고** (`monitor/app.py:207`, `gateway/kis_api.py:478`).
- 부수 상태 모두 Redis: `scale_out:{code}`, `rsi_sold:{code}`, `profit_floor:{code}`. 포지션 소멸/신규매수/전량매도 시 cleanup (`monitor/app.py:503-556`).

#### 다층 청산 규칙 12종 (우선순위 평가, 첫 매치 반환 — `exit_rules.py:398-424`)

| # | 규칙 | 조건 | 매도량 |
|---|------|------|--------|
| 0 | Hard Stop | profit ≤ -10% (gap-down 안전망) | 100% |
| 1 | Profit Floor | 고점수익 ≥15% 도달 후 → profit < 10% | 100% |
| 2 | Profit Lock L2/L1 | ATR기반 동적 trigger(L2 3~5%/L1 1.5~3%) 도달 후 floor 이탈 | 100% |
| 2.5 | Breakeven Stop | 고점수익 ≥+3% 후 → profit < +0.3% | 100% |
| 3 | ATR Stop | price ≤ buy - ATR×(2.0×macro); MACD약세 ×0.75/death cross ×0.8 타이트닝 | 100% |
| 4 | Fixed Stop | profit ≤ -6%(config) / 시간기반 조임 최대 -2%p | 100% |
| 5 | **Trailing Take-Profit** | 고점수익 ≥ activation 도달 후 현재가 ≤ 고점×(1-drop%) | 100% |
| 6 | Scale-Out | 국면별 분할익절 레벨 도달 (+최소거래 가드) | 15~25% |
| 7 | RSI Overbought | RSI ≥75 & profit ≥3% (trailing 활성 시 스킵) | 50% |
| 8 | Profit Target | profit ≥10% (trailing 비활성일 때 폴백) | 100% |
| 9 | Death Cross | 5MA/20MA 하향돌파 & profit < -1% (BULL 국면 비활성) | 100% |
| 10 | Time Exit | 보유 ≥30일 | 100% |

#### 트레일링 익절(rule 5)의 정밀 메커니즘 (`exit_rules.py:223-267`)
1. `trailing_enabled` 확인.
2. **activation**: 기본 `trailing_activation_pct`=5%. `high_profit_pct`(고점 수익률)가 activation 이상이어야 발동. MACD 약세 → activation×0.8, death cross → ×0.7 (조기 발동).
3. **drop threshold (국면별)**: STRONG_BULL/BULL 3.0%, SIDEWAYS/BEAR `trailing_drop_from_high_pct`=3.5%, STRONG_BEAR 4.0%.
4. **트레일링 스톱 가격** = `high_watermark × (1 - drop%/100)`. 현재가가 이 선 이하 **그리고** profit ≥ `trailing_min_profit_pct`(3%)이면 전량 매도.

→ 고점은 **Redis `watermark:{code}` 가격값**, 평단가는 **KIS 잔고 평단**. 둘로 `high_profit_pct`(=(hw-buy)/buy) 산출. 트레일링은 절대가격(hw) 기준, profit_lock(rule 2)은 수익률(high_profit_pct) 기준 — 두 트레일링 계열이 병존.

#### 실시간 가격 피드
Gateway WebSocket(`H0STCNT0` 체결가) → `kis:prices` Stream → monitor `XREADGROUP`. monitor는 `count=50, block=2000ms`로 읽고 **읽자마자 XACK(at-most-once)** 후 처리 (`monitor/app.py:714-741`). 장중(09:00-15:30·거래일)에만 처리, 장외 60초 sleep.

### 1.5 청산 주문 — sell-executor

`process_signal()` (`seller/executor.py:108-147`): 장중체크 → emergency stop → 포지션 검증(KIS 잔고에 있는지) → 분산 락(`lock:sell:` EX30) → `_execute_sell`. **MANUAL/FORCED_LIQUIDATION은 장중·stop 체크 우회**.
- 매도는 항상 **시장가** (`seller/executor.py:200-204`).
- 수량 검증: `min(order.quantity, position.quantity)` — 보유 초과 방지.
- 미체결 시 취소 후 재확인(이미 체결됐을 수 있음) (`seller/executor.py:233-255`).
- STOP_LOSS/DEATH_CROSS/BREAKEVEN_STOP → 재매수 쿨다운 `stoploss_cooldown:` (3일); 모든 매도 → `sell_cooldown:` (24h) (`seller/executor.py:31, 271-275, 316-332`).
- FORCED_LIQUIDATION은 체결 폴링 확장(10회×5초).

### 1.6 주문 안정성 — kis-gateway

- **중앙 프록시**: 모든 서비스는 KIS를 직접 안 치고 Gateway 경유 → rate limit·circuit breaker를 한 곳에서.
- **Rate Limit**: slowapi `Limiter`, key를 **IP가 아닌 `"global_kis_account"`** 로 고정 → 시세 19/초, 매매·잔고 5/초 (`gateway/app.py:52, 190, 292`).
- **Circuit Breaker**: pybreaker `fail_max=20, reset_timeout=60` — 모든 KIS 호출을 `_circuit_breaker.call()`로 감쌈, open 시 503 (`gateway/app.py:62-66`).
- **재시도/토큰**: `_request()` 가 연결오류 1회 재시도, 401/403/500 시 토큰 강제 재발급 후 1회 재시도 (`gateway/kis_api.py:138-157`). 토큰은 파일 캐시(24h) (`kis_api.py:82-110`).
- **WebSocket 복원력**: exponential backoff 60→600초, 30초 이상 안정연결 후 끊기면 backoff 리셋, 재연결 시 approval_key 갱신, hot subscribe(재시작 없이 종목 추가) (`gateway/streamer.py:210-276, 121-134`).
- **REST 폴링 폴백**: WebSocket 불가 시 `KISRestPoller`가 동일 인터페이스(duck typing)로 3초 주기 폴링 → 같은 `kis:prices` Stream 발행 (`gateway/poller.py`).
- **DB 폴백**: daily-prices는 KIS 실패 시 DB에서 조회 (`gateway/app.py:213-228`).

### 1.7 실거래 데이터 검증 — era 분리 귀속 (v2 MariaDB `jennie_db.trade_logs`)

**era 경계 (orchestration 확정 + execution DB 재검증):** v2 stream 아키텍처 라이브는 **2026-02-19 v1→v2 컷오버** 후 약 2주뿐. 이후 retire(04-18)까지 emergency_stop으로 실거래 0. id 연속(1–437) + 레거시 `tradelog`(371행, max ts 02-19 02:21) 대조로 경계 확정:

| era | id 범위 | BUY | SELL | 비고 |
|---|---|---|---|---|
| v1-ETL | 1–370 | 160 | 210 | 레거시 tradelog ETL 이관분 |
| v2-native seed | 371–376 | 6 | 0 | ts 전부 02-20 00:00:00 (date-only) — 초기 포지션 시딩, 라이브 실행경로 아님 |
| **v2-native LIVE** | 377–437 | **26** | **35** | v2 stream 아키텍처 실거래, 02-20~03-21 |

→ **`trade_logs` SELL 245건 중 210건(86%)이 v1-ETL.** 이전 판본이 "v2 트레일링 검증됨"의 근거로 인용한 통합 수치 대부분이 v1 거래. 정정한다. (※ 본 분석 소스 테이블 = `trade_logs` 단일. era 분류는 직접 id/timestamp 조회로 확인 — backtest_tradelog/별 집합 미사용.)

**v1-ETL 청산 (210 SELL) — v2가 아닌 레거시 성과:**
Trailing 18·Scale-Out 38·RSI 18·Profit Lock 15·Profit Floor 3·Profit Target 3·Stop Loss 42·Death Cross 4·Manual 69. Trailing 평균 +7.54%(+18.5M KRW), Scale-Out 38건 승률 100%(+11.8M), +23.7% 단일 winner — **전부 v1.** 합산 실현 +30.5M KRW, 승률 70.1%(기록분), 평균 보유 2.94일.

**v2-native LIVE 청산 (35 SELL) — v2 stream 아키텍처의 실적 전부:**

청산사유는 `trade_logs.reason` 컬럼에 enum 문자열로 기록됨(`seller/app.py` `_persist_sell` → `reason=str(order.sell_reason)`). ※ 주의: SELL 행의 `strategy_signal` 컬럼은 청산사유가 아니라 **진입 전략**을 담는다(매도 시 직전 BUY 로그를 역참조 — `seller/app.py:115-126`). orchestration 초기 보고가 `strategy_signal`을 보고 "v2 SELL은 청산사유 미기록"이라 했으나, **청산사유는 `reason` 컬럼에 정상 기록돼 있다.** id 377–437 SELL 35건 전수 분류:

| reason | 건수 | 승 | profit_pct 범위 |
|---|---|---|---|
| TRAILING_STOP | 7 | 7 | +0.32 ~ +13.26% (5건 스크래치 <+1.2%, 2건만 실질: +13.26 / +8.68) |
| PROFIT_TARGET | 7 | 7 | +2.83 ~ +11.7% |
| STOP_LOSS | 6 | 0 | -5.01 ~ -5.33% (5% 고정손절 군집) |
| DEATH_CROSS | 3 | 0 | -0.06 / -0.15 / -1.07% |
| MANUAL | 6 | 2 | -1.29 ~ +0.19% |
| MANUAL_SYNC | 6 | — | profit_pct NULL — 포지션 동기화, 실청산 아님 |

집계 (profit_pct 기록된 29건; MANUAL_SYNC 6건 제외):
- **전체: 16승 13패, 승률 55.2%, 평균 profit_pct +0.97%, 합산 실현 약 −344,722원 (net 소폭 손실).**
- 시스템 자동청산만(Trailing+ProfitTarget+StopLoss+DeathCross 23건): 14승 9패, 승률 60.9%, 평균 +1.37%.
- holding_days: 35건 전부 NULL (§3.1 — 컷오버 시점부터 미기록).

데이터가 말하는 것:
- **청산 규칙은 사유별로 정상 발동했다.** 익절 계열(Trailing 7·Profit Target 7) 14건 전부 수익, 손절 계열(Stop Loss 6·Death Cross 3) 9건 전부 손실 — 다층 청산이 2주 표본에서도 깔끔히 분리 작동. STOP_LOSS는 -5%선에 타이트.
- **그런데 net은 소폭 마이너스(−34만원).** 작은 익절 다수(+0.3~3%대) vs -5% 손절 6건 — 손절 절대손실이 잔익절을 약간 상회. v2-native 트레일링 7건 중 5건이 스크래치(거의 안 움직인 포지션을 트레일링이 흘려보냄), 실질 포착은 2건뿐.
- **표본이 통계적으로 빈약.** 청산 35건·시스템 23건·트레일링 7건, 단일 2주. `[[feedback_single_day_overfit]]` 원칙대로 — 이 표본으로 v2 stream 아키텍처 청산 설계의 우열을 **데이터로 판정할 수 없다.** 확정 가능한 것은 ① 청산 규칙이 사유별로 정상 발동 ② 2주 net 약 −34만원 ③ holding_days 미기록으로 시간기반 청산은 죽어 있었음 — 그뿐.
- v2-native에서 **Scale-Out·RSI·Profit Lock·Profit Floor는 0건 발동** — 2주 기회 부재인지 미작동인지 데이터만으로 불명 (별도 조사 필요).
- **Time Exit은 v1·v2 통틀어 245건 중 0건 발동** — §3.1 dead-code 진단과 일치.

**귀속 결론 (★):** "v2의 동적 트레일링/다층 청산이 좋았다"의 인상적 실적(트레일링 +7.5%, Scale-Out 100% 승률, +23.7% winner, +30M KRW)은 **전부 v1-ETL(레거시) 거래**다 — v2 stream 아키텍처에 대한 주장이 아니다. v2 stream 아키텍처 자체의 라이브 실적은 **청산 35건 / 2주 / net −34만원 / 승률 55%**가 전부이며, 소표본이라 설계 우열을 데이터로 판정 불가. §2 "v2가 잘한 것"은 **코드 설계(file:line) 평가로만 유효**하고, 프로덕션 성과 검증은 미완이다.

---

## 2. v2가 잘한 것 (★ 핵심)

> **범위 명시:** 이하 §2는 v2 repo **코드 설계**에 대한 평가다(file:line 근거). v2-native 라이브가 2주뿐이라(§1.7) 이 설계들이 프로덕션 성과를 냈다는 **데이터 검증은 미완**이다. "잘 설계됐다"와 "잘 작동했다"를 구분해 읽을 것.

### 2.1 청산이 진입보다 정교했다 — "팔 줄 아는" 시스템
12종 청산 규칙이 **명시적 우선순위 1축으로 정렬**돼 평가된다 (`exit_rules.py:398-424`). 단순 손절·익절이 아니라 한 종목 라이프사이클의 단계별로 다른 보호막이 깔려 있다:
- 초기(미실현 손실): Hard Stop(-10%) / ATR Stop / Fixed Stop.
- 본전 회복 직후: Breakeven Stop(+3% 찍으면 +0.3% 바닥 — 수수료만 건지고 무손실 탈출).
- 수익 구간: Profit Lock L1/L2(ATR로 동적 trigger) → Trailing TP → Profit Floor(15%+ 수익을 10%에 락).

이 **계층화**가 v2의 가장 견고한 설계다. 각 규칙이 작은 순수함수(`check_*`)로 분리돼 단독 테스트·추론이 가능하고, `evaluate_exit`는 그저 순서대로 호출만 한다. 한 규칙을 바꿔도 다른 규칙에 새지 않는다.

### 2.2 동적·국면 적응형 트레일링
트레일링이 고정 %가 아니다:
- **drop %가 국면별**: 강세장 3.0%(수익 더 태움) / 횡보·약세 3.5% / 강한약세 4.0% (`exit_rules.py:246-252`).
- **경고 지표로 조기 발동**: MACD 약세/death cross 감지 시 activation을 ×0.8/×0.7로 낮춰 트레일링을 **더 일찍** 켠다 (`exit_rules.py:237-240`). 같은 신호가 ATR Stop 타이트닝(×0.75/×0.8)에도 연동 (`exit_rules.py:163-167`).
- Profit Lock의 trigger 자체가 **종목 변동성(ATR)에 비례** — 변동성 큰 종목엔 느슨, 작은 종목엔 타이트 (`exit_rules.py:104-128`).

"고점 추적 후 일정 % 하락 시 매도"라는 기본형을 **변동성·국면·기술적 경고 3개 축으로 변조**한 것 — 단순 트레일링보다 한 단계 위.

### 2.3 진입/청산 감시의 깔끔한 분리 + 단일 피드
scanner와 monitor가 **같은 `kis:prices` 스트림을 독립 consumer group으로** 소비한다 (`scanner-group` vs `monitor-group`). 진입 로직과 청산 로직이 코드·프로세스·장애 도메인 모두에서 분리돼 있으면서 시세 인프라는 하나만 둔다. monitor가 죽어도 scanner는 살고, 그 반대도 성립.

### 2.4 Gateway = 단일 KIS 통제점
모든 KIS 호출이 Gateway 1곳을 지난다. 그래서 **rate limit·circuit breaker·토큰관리·재시도·DB폴백을 한 번만 구현**하면 전 서비스가 보호받는다. rate limit key를 IP가 아니라 `"global_kis_account"`로 고정한 건 정확한 통찰 — KIS 제한은 계정 단위지 IP 단위가 아니다 (`gateway/app.py:52`). WebSocket↔REST 폴링을 duck typing 인터페이스로 호환시켜 무중단 폴백을 만든 것도 견고.

### 2.5 상태 외부화 — 재시작 안전
high watermark, scale-out 레벨, rsi_sold, profit_floor, 각종 쿨다운이 전부 **Redis에 TTL과 함께** 저장된다. 서비스가 재시작돼도 트레일링 고점이 살아남는다 (`watermark:` TTL 30일). 인메모리만 썼다면 재시작마다 트레일링이 리셋돼 고점을 잃었을 것. watermark는 추가로 5분마다 DB 동기화 — 캐시 손실 대비 2중화.

### 2.6 실행 경로의 방어적 다단계 검증
buy-executor 9단계·sell-executor의 단계별 게이트, **분산 락**(동일 종목 동시주문 차단), 미체결 시 "취소 → 재확인(취소-체결 경합 처리)" 패턴 (`buyer/executor.py:287-309`, `seller/executor.py:233-255`)은 실거래에서 실제로 터지는 race를 의식한 코드다. emergency stop / pause / dryrun 플래그가 scanner·monitor·executor 전 계층에 일관되게 박혀 있어 한 키로 전체를 멈출 수 있다.

---

## 3. v2가 못한 것 (간략 · 증거)

### 3.1 ★ 시간 기반 청산이 실거래 경로에서 죽어 있다 — v2 스스로의 회귀
`Position.bought_at`은 `datetime | None = None`이고 (`domain/portfolio.py:25`), **코드 전체에서 set하는 곳이 없다** (grep: 소비 3곳 전부 monitor, 생산 0곳). monitor가 쓰는 포지션은 KIS 잔고 API에서 오는데 그 응답에 매수일시가 없다 (`gateway/kis_api.py:469-484` — `bought_at` 미포함).
→ `_evaluate_position`의 `holding_days`는 **항상 0** (`monitor/app.py:238-241`).
→ **Rule 10 Time Exit(30일)은 절대 발동 안 함**. Rule 4 Fixed Stop의 시간기반 조임(`time_tighten`)도 `holding_days > start_days`가 영영 거짓이라 **적용 안 됨**. SellOrder.holding_days도 항상 None.
문서·docstring은 12개 규칙을 광고하지만 라이브에선 사실상 **10.5개**. 30일 넘게 물린 포지션을 시간만으로 끊는 안전망이 없다.

**데이터 증거 — v2-native는 컷오버 첫날부터 holding_days가 없었다.** `trade_logs`의 SELL `holding_days` 채움률을 era로 분리하면: **v1-ETL 210건은 ~96% 채움** vs **v2-native LIVE 35건은 전부 NULL(미기록)**. 즉 v1은 보유일수를 정상 기록했고, v2의 stream 아키텍처는 **2026-02-19 v1→v2 컷오버 시점부터** `bought_at` 공급이 끊긴 채 출발했다(점진 회귀가 아니라 컷오버 자체의 결손). 원인은 위 본문대로 — v2 monitor가 포지션을 KIS 잔고 API에서 받는데 그 응답에 매수일시가 없다. v2는 2주 라이브 내내 시간기반 청산이 죽은 채 돌았고, Time Exit 실발동 0/245건으로 확정. orchestration의 era 분할 보고와 일치.

### 3.2 틱의 `high`(당일 고가) 필드를 버린다
Gateway streamer는 체결가와 함께 당일 고가를 `kis:prices`에 실어보낸다 (`streamer.py:316, 321`). monitor `process_tick(code, price, high)`는 `high`를 인자로 받지만 **본문에서 한 번도 안 쓴다** (`monitor/app.py:153-182`). high watermark는 오로지 "관찰된 틱 가격의 최댓값"으로만 만들어진다. at-most-once(XACK 먼저, `monitor/app.py:727`)라 처리 중 크래시·과부하로 틱을 흘리면 **진짜 장중 고점을 놓치고**, 트레일링 스톱이 실제보다 낮게 잡힌다. 공짜로 들어온 정답(`high`)을 안 쓴 셈.

### 3.3 매수 부분체결 잔량을 취소하지 않는다 → entry under-count
`confirm_order`는 폴링 윈도우(5회×3초) 만료 후 부분체결이면 그 부분 수량만 반환한다 (`infra/kis/client.py:114-125`). buy-executor는 이를 받아 `actual_qty = fill["filled_qty"]`(부분)로 기록하고 **미체결 잔량을 KIS에 그대로 살려둔다** (`buyer/executor.py:281-285`). 잔량이 나중에 체결되면 KIS 실보유 > v2 기록. → MEMORY의 "Entry 부분체결 under-count"(241560 5-14 KIS 113 vs v3 72)와 **동일 패턴이 v2 코드에 이미 존재**. v3가 물려받은 버그이고 e708586에서야 고쳤다.

### 3.4 진입 전략 파라미터가 사실상 하드코딩·미검증
`detect_momentum`의 7%, `detect_dip_buy`의 국면별 조정폭, `detect_momentum_continuation`의 LLM≥65 등 임계값이 함수 시그니처 기본값으로 박혀 config화돼 있지 않다 (`strategies.py:163-228`). `overextension` 게이트 주석은 Grid Search 근거를 달았지만(`risk_gates.py:254-264`) 대부분 전략 임계값엔 그런 근거 추적이 없다.

### 3.5 momentum 지정가 취소 휴리스틱의 모호함
"`cancel_order`가 False면 = 이미 체결됨"으로 단정 (`buyer/executor.py:380-385`). 취소 실패는 네트워크·API 오류로도 발생할 수 있어 미체결을 체결로 오판할 여지. 후속 `confirm_order`가 막아주긴 하나 분기 의도가 불명확.

### 3.6 monitor의 평가는 "마지막 본 틱"에만 반응
`process_tick`은 그 틱에 대해서만 `evaluate_exit`를 돈다. 종목이 watchlist/구독에서 누락되거나 틱이 끊기면 그 종목 청산 평가가 멈춘다. 5분 `refresh_positions`가 포지션 목록은 갱신하지만 **가격 평가를 강제로 한 바퀴 돌리진 않는다** — 조용한 무감시 구간이 생길 수 있다.

---

## 4. v3 비교 훅 (2단계 점검 체크리스트)

v3(prime-jennie-runtime)가 아래 각 항목을 **유지/개선/퇴보**시켰는지 확인:

**청산 규칙**
1. v3에 12종 다층 청산 규칙이 살아 있나? 우선순위 1축 정렬·순수함수 분리 구조가 유지됐나, 아니면 sheet/trailing 모델로 단순화하며 규칙이 사라졌나?
2. 트레일링 익절의 **국면별 drop %**(3.0/3.5/4.0)와 **MACD·death cross 조기발동**(×0.8/×0.7)이 v3에 있나?
3. Profit Lock의 **ATR 기반 동적 trigger**가 v3에 있나, 고정 %로 퇴보했나?
4. Breakeven Stop(+3%→+0.3%), Profit Floor(15%→10%) 같은 단계별 보호막이 v3에 있나?
5. **시간 기반 청산**: v3는 `bought_at`/보유일수를 실제로 채우나? v2의 holding_days=0 죽은 코드 버그를 v3가 고쳤나, 그대로 물려받았나? (position_sheets에 entry 시각 컬럼 존재 여부 확인)

**고점·평단 추적**
6. v3는 high watermark를 어디 저장하나(Redis/DB/sheet)? 재시작 생존성은? TTL 만료 리스크는?
7. v3는 틱의 당일 고가(`high`)를 watermark에 반영하나, v2처럼 버리나?
8. v3 평단가의 single source of truth는? (v2 = KIS 잔고 평단 직접 사용)

**진입·체결**
9. v3 진입 전략 수·게이트 수 대비 v2(전략 9·게이트 13)? 차등 게이트 우회(conviction/ORB/gap-up) 개념이 남아 있나?
10. **부분체결 처리**: v3는 매수 부분체결 잔량을 취소하나? (e708586이 fix — v2 버그를 v3가 늦게라도 잡았는지, sell 경로도 동일한지)
11. ATR risk-parity 사이징(1R=ATR×2, 1% risk, Portfolio Heat 5%, Smart Skip)이 v3에 유지됐나?

**아키텍처·안정성**
12. v3도 진입감시/청산감시를 분리된 프로세스+독립 consumer group으로 두나, 한 오케스트레이터로 합쳐 단일 장애점이 생겼나?
13. v3에 KIS 단일 통제점(Gateway 동급)이 있나? rate limit(계정 단위 key)·circuit breaker·토큰관리·WebSocket↔폴링 폴백이 유지됐나?
14. emergency stop / pause / dryrun 글로벌 플래그가 v3 전 계층에 일관되게 박혀 있나?
15. v3의 청산 트리거가 v2처럼 실시간 틱 기반인가, 폴링/배치 기반으로 바뀌어 반응 지연이 생겼나?

---

### 못 알아낸 것 (정직 고지)
- 손절선 운영값: `config.sell.stop_loss_pct` 기본은 6.0, `exit_rules.py` docstring은 -5% 표기. 실데이터상 손절은 시기별로 -5%·-7% 양쪽 임계값에서 발동(`reason` 문자열 근거) — config가 운영 중 바뀐 흔적. 시점별 정확한 `.env` 값은 미확인.
- v2 운영 시 `streamer_mode`가 websocket이었는지 polling이었는지 (config 기본 websocket) 실제 배포값 미확인.
- BUY/SELL 불일치는 era 분리로 대부분 해소: v1 BUY 157/SELL 209(ETL 윈도우 이전 개시 포지션을 v1이 매도), v2-native는 BUY 35/SELL 35로 정확히 균형. v2-native 1:1 라이프사이클 추적(매수→청산 페어링)은 미수행 — orchestration 영역.
- BUY `strategy_signal`의 레거시 값(RSI_OVERSOLD 22, TREND_UPWARD 9 등)은 v1 전략으로 추정 — v2-native 35 BUY의 전략 분포는 별도 미집계(selection 에이전트 영역과 중복 가능).
- v2-native에서 Scale-Out/RSI/Profit Lock/Profit Floor 미발동(0건)이 "2주 표본 기회 부재" 때문인지 "규칙 비활성·미작동" 때문인지 — 데이터만으로 구분 불가. monitor 로그 또는 config 운영값 확인 필요.
