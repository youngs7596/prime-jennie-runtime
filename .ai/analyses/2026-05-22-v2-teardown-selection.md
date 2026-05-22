# v2 Teardown — 종목 선정 두뇌 (selection)

작성: 2026-05-22 / 에이전트: selection / v2-teardown 1단계
대상: v2 = `prime-jennie` (로컬, 마지막 커밋 2026-04-21 `785641e`, 브랜치 development)
원칙: 코드·데이터가 진실. 모든 결론 file:line / 실DB 인용. 미화·폄하 금지.

조사 범위: `prime_jennie/services/scout/*`, `services/council/*`, `services/scanner/*`(handoff 한정),
`domain/{scoring,macro,enums,config,sector*}.py`, `prompts/`, v2 MariaDB `jennie_db`(부활 후 접속)
+ v3 postgres 의 v2 ETL 잔존 테이블. 핵심 실데이터: `daily_quant_scores` 20,996행
(2026-03-18~04-17) / `watchlist_histories` 9,479행 / 전신 피드백 테이블 3종(§1.9).
못 본 부분은 §5 에 명시.

---

## 1. v2가 한 일 · 메커니즘 (file:line)

### 1.1 Scout 파이프라인 — 하루 7회 도는 단일 오케스트레이터

스케줄: Airflow `scout_job_v1`, cron `30 8-14 * * 1-5` → **평일 08:30~14:30 매시 1회 = 7회/일**
(`dags/scout_job_dag.py:18`). `/trigger` 동기 호출, 60분 timeout.

`run_pipeline()` (`scout/app.py:122-296`) 가 순차 실행하는 단계 (docstring 은 "8단계"라 하지만
실제 서브페이즈는 ~16개):

| Phase | 내용 | 코드 |
|---|---|---|
| 0 | 이전 watchlist 로드 (히스테리시스용) | `app.py:136-145` |
| 1 | Universe 로딩 | `universe.py:18-72` |
| 1.5 | RAG 후보 발굴 | `rag_retriever.py:79-129` |
| 2 | Enrichment (KIS+DB 병렬) | `enrichment.py:67-200` |
| 2.5 | 섹터 20일 모멘텀 | `app.py:170-186` |
| 2.5b | 섹터별 PBR/PER 백분위 | `app.py:435-481` |
| 2.5 | RAG 뉴스 프리페치 | `rag_retriever.py:132-254` |
| 2.9 | 현재가 하한 필터 (1만원) | `app.py:197-204` |
| 3 | Quant Scoring (7팩터) | `quant.py:50-96` |
| 4 | LLM Analysis (1-pass) | `analyst.py:33-91` |
| 4.5 | MA smoothing (3일 이동평균) | `app.py:406-432` |
| 5 | Sector Budget | `sector_budget.py` |
| 6 | Selection (greedy + 히스테리시스) | `selection.py:23-169` |
| 6.5~9 | DB/Redis 저장, 정리 | `app.py:266-289` |

산출물: `HotWatchlist` → Redis `watchlist:active` (TTL 24h) + DB `watchlist_histories` /
`daily_quant_scores` (run_id 별 이력).

### 1.2 Universe — 시총순 KOSPI Top 200

`StockRepository.get_active_stocks(market="KOSPI")` → `min_market_cap` 500억 필터 → 시총 내림차순 →
`universe_size` 200개 컷 (`universe.py:28-65`, `config.py:178-193`). 기본 `universe_market="KOSPI"`.
RAG discovery(`rag_retriever.py`) 가 Qdrant 4토픽 쿼리(실적/수주/신사업/주주환원)로 universe 밖
종목을 추가 편입 가능(`app.py:156-160`).

실데이터: `daily_quant_scores` 기준 1런당 채점 종목 ~126개(7런 = ~880행/일). universe 200 →
가격/시총 필터 후 ~126.

### 1.3 Quant Scoring — 7 서브팩터 결정론 (quant.py, 490줄, 단일 파일)

`V2_WEIGHTS` (`quant.py:27-35`): 모멘텀 20 / 품질 20 / 가치 20 / 기술 10 / 뉴스 10 / 수급 20 /
섹터모멘텀 10. 합산 후 `max(0, min(100, total))` 캡 (`quant.py:80-81`).

- **모멘텀(0-20)**: RSI(regime 연동 — BULL 에선 70-80 무페널티) + 6M/3M/1M 가격모멘텀 +
  눌림목 감지(6M↑·1M↓) + earnings revision 보너스 (`quant.py:102-163`)
- **품질(0-20)**: ROE(forward 컨센서스 우선) + PBR + PER, 섹터 백분위 상대평가 우선·절대평가 폴백
- **가치(0-20)**: PER 할인 + PBR + 52주 고점 대비 (고점 근접 = 가점)
- **기술(0-10)**: 이평선 정배열 + 거래량 5일/20일 비율
- **뉴스(0-10)**: 14일 뉴스 감성 평균 → 선형매핑
- **수급(0-20)**: 외인/기관 60일 순매수 합 + 외인 보유비율 추세
- **섹터모멘텀(0-10)**: 섹터 20일 평균수익률

데이터 < 20일이면 전 팩터 중립값 `V2_NEUTRAL`(합 55) 반환 (`quant.py:69-70, 474-490`).

### 1.4 LLM — "Calibrator(보정자)" 1-pass, ±15 클램프

`analyst.run_analyst()` (`analyst.py:33-91`):
1. quant ≥ 25 인 종목만 LLM 호출 (`app.py:231`), 동시성 `Semaphore(20)`
2. LLM 은 0-100 raw score + grade + reason 반환 (`ANALYST_RESPONSE_SCHEMA`, temp 0.3)
3. **`_clamp_score`: quant ± 15pt 가드레일** (`analyst.py:235-239`, `config.py:130`)
4. `risk_tag` 은 **LLM 아님 — 코드가 결정** (`classify_risk_tag`, `analyst.py:94-137`):
   DISTRIBUTION_RISK(고점+RSI>70+외인·기관 동시이탈) / CAUTION / BULLISH / NEUTRAL
5. DISTRIBUTION_RISK → `veto_applied` → `trade_tier = BLOCKED` (`analyst.py:72-74`)
6. LLM 호출 실패 시 quant 점수로 fallback (`analyst.py:55-61`)

프롬프트는 `analyst.py:_build_prompt` 가 인라인 생성. `prompts/analyst/unified_analyst.txt` 는
`{feedback_section}` 등 placeholder 를 가진 별도 버전으로, **코드에서 미참조**(grep 결과
`config.py:129` 의 이름 일부 매치뿐) — stale.

### 1.5 결정론 ↔ LLM 분리 구조

| 단계 | 성격 |
|---|---|
| Quant 7팩터 | 100% 결정론 |
| risk_tag 분류 | 100% 결정론 (코드 룰) |
| trade_tier (TIER1≥60 / TIER2≥40 / BLOCKED) | 결정론 (`analyst.py:242-249`) |
| LLM | quant ±15 보정만 — clamp 가 비결정론을 물리적으로 가둠 |
| MA smoothing, hysteresis, greedy 선정 | 100% 결정론 |

### 1.6 Macro Gate — Council (소프트 게이트)

`MacroCouncilPipeline` 3-step LLM (`council/pipeline.py`): Strategist(DeepSeek) →
Risk Analyst(DeepSeek) → Chief Judge(Claude). 출력 `MacroInsight` → `_update_trading_context`
(`jobs/app.py:1886-1940`) 가 `sentiment_score` → `MarketRegime` 매핑(≥70 STRONG_BULL … <25
STRONG_BEAR) 하여 Redis `macro:trading_context` 저장.

Scout 가 이 context 를 쓰는 방식 (`app.py:167`):
- `market_regime` → 모멘텀 RSI 페널티 보정 (`quant.py:66, 119-128`)
- `avoid_sectors` → 해당 섹터 강제 COOL (cap↓), `favor_sectors` → COOL→WARM 승격
  (`sector_budget.py:67-78`)

**중요: macro 는 scout 를 멈추지 못함.** 약세장이든 STOP 이든 scout 는 무조건 watchlist 25종목을
생성한다 (`app.py` 에 emergency-stop 체크 없음 — `scanner` 에만 `trading_flags:stop` 존재).
macro 는 RSI 보정·섹터 cap 만 건드리는 soft tune.

### 1.7 Selection — Greedy + 히스테리시스 + 섹터 cap

`select_watchlist()` (`selection.py:23-169`):
1. MA score 5점 버킷 → 동일 버킷 내 시총 내림차순 정렬 (`_sort_key`, `selection.py:60-67`)
2. BLOCKED 제외, `is_tradable` 만
3. **히스테리시스**: MA score ≥ `entry_threshold`(62) 신규진입 / 55~62 + 이전 WL 에 있으면 유지 /
   < `exit_threshold`(55) 이면 제거 (`selection.py:73-100`)
4. 섹터 cap 지키며 greedy 선정 (cap = budget tier 별 HOT 5/WARM 3/COOL 2~3, 반도체 override 4)
5. `max_size` 미달 시 skip 종목에서 backfill (`selection.py:122-126`)

### 1.8 후보 → 매수 경로

v2 엔 명시적 "buy sheet" 개념 **없음**(`position_sheet`/`buy_sheet` grep = 0건). 경로:

```
watchlist:active (Redis, scout 산출)
  → scanner (장중 tick 모니터링, conviction/momentum/dip/ORB/gap-up 전략 탐지)
  → stream:buy-signals
  → buyer/executor (hard_floor 40 게이트: hybrid<40 reject — executor.py:140)
  → 매수 주문
```

selection 점수가 scanner 에서도 게이트로 작동: `trade_tier == BLOCKED` 차단
(`risk_gates.py:210-214`), conviction entry 는 `hybrid≥70`(SIDEWAYS 75)·`llm≥72` 요구
(`strategies.py:304-316`), momentum_continuation 은 `llm≥65` (`strategies.py:221`).
즉 watchlist 가 곧 매수 후보집합이고, scanner 가 장중 타이밍 + 점수 컨빅션 게이트를 더한다.

### 1.9 v2가 전신(my-prime-jennie)에서 버린 것 — 선정 피드백 3종

v2 DB(MariaDB `jennie_db`, 부활 후 접속) 에는 v2 코드가 **전혀 참조하지 않는**(grep 0건,
`models.py` 미등재) 대문자 컬럼명 테이블이 잔존 — 전신 `my-prime-jennie` 의 선정 피드백
서브시스템이다. 모두 v2 가동 이전·전환기에 데이터가 끊김:

| 테이블 | 행수 | 데이터 기간 | 정체 |
|---|---|---|---|
| `shadow_radar_log` | 16,800 | 2025-12-17~2026-02-19 | 탈락 후보 추적 (REJECTION_STAGE/REASON, HUNTER_SCORE) |
| `factor_performance` | 104 | 2025-12-05~12-20 | 조건별 실증 승률 (CONDITION_KEY, WIN_RATE, SAMPLE_COUNT) |
| `optimization_history` | 62 | 2025-11-15~11-25 | AI 파라미터 최적화 (AI_DECISION, MDD, 백테스트) |

- **shadow_radar_log**: 탈락 종목을 단계(HUNTER 16,284 / JUDGE 431 / UNIFIED_ANALYST 85)·
  사유와 함께 로깅. UNIFIED_ANALYST 85행은 v2 초기 파이프라인에도 잠깐 살아있었음을 보여줌.
  → v2 는 별도 rejection 로그를 폐기하고 `daily_quant_scores`(전 종목 점수 + `is_final_selected`)
  로 흡수. 더 깔끔한 통합 (→ §2-H).
- **factor_performance**: `rsi_oversold_30` 승률 93%(n=15), `volume_surge_2x` 82%(n=33) 식의
  조건별 실증 승률 테이블. v2 `QuantScore.matched_conditions/condition_win_rate/
  condition_confidence` 필드(`scoring.py:25-27`)가 바로 이 시스템의 **잔존 스키마 훅** —
  v2 는 필드만 남기고 테이블·집계 로직을 폐기. 단, 전신의 표본은 n=8~35 로 통계력이 약했고
  (CONFIDENCE 대부분 LOW/MID) — MEMORY `feedback_single_day_overfit` 관점에서 이 실증
  루프 자체가 과적합 취약. v2 가 버린 게 명백한 퇴보라 단정하긴 어려움.
- **optimization_history**: MDD/수익률 기준 AI 가 파라미터를 바꾸던 루프. v2 폐기.

요지: v2 는 전신의 "실증 피드백 + AI 자동튜닝" 루프를 걷어내고 **손으로 튜닝한 7팩터
휴리스틱 + 오프라인 backtest 서비스**(`services/backtest/`, 라이브 selection 에 미연결)로
단순화했다. §2-A/B 의 단순·결정론 강점과 §3 의 "가중치 실증 미검증" 약점이 동시에 여기서 옴.

---

## 2. v2가 잘한 것 (핵심)

### A. 결정론과 LLM 의 물리적 분리 — 실데이터로 검증됨
LLM 을 ±15 clamp 보정자로 격리하고 risk_tag·trade_tier·정렬·선정은 전부 코드가 결정.
**실측 증거** (`daily_quant_scores` 20,996행):
- `avg(hybrid_score − total_quant_score) = +1.12` — LLM 이 최종점수를 평균 1.1점밖에 못 움직임
- `DISTRIBUTION_RISK` veto 발동 = **20,996행 중 1행**
- LLM 실패 시 quant fallback 보장 (`analyst.py:55-61`)

결과적으로 선정 결정의 ~99%가 결정론이다. 이는 비결정론이 edge case 로 새어나갈 표면적을
구조적으로 최소화한 설계 — MEMORY 의 `feedback_prompt_control_limit`("결정론은 별도 layer 에")
원칙과 정확히 정합한다. clamp 는 prompt 가 아니라 코드 layer 의 가드레일이다.

### B. 스코어링이 단순하고 한 파일에서 추적 가능
7 서브팩터, 각 0-20/0-10, 선형매핑(`_linear_map`) + 명시적 점수 버킷. 가중치는 `V2_WEIGHTS`
dict 한 곳. `quant.py` 490줄 전체가 한 파일 — 어떤 종목이 왜 그 점수인지 코드만 읽고 재구성 가능.
숨은 상태·외부 호출·서비스 분산 없음.

### C. 데이터 부재에 견고
모든 서브팩터가 데이터 None 일 때 명시적 중립값을 반환(`V2_NEUTRAL`, `quant.py:39-47`).
Enrichment 는 종목별·필드별 try/except 격리(`enrichment.py:101-191`) — 한 종목 한 필드가
실패해도 그 필드만 None, 파이프라인은 안 멈춤. RAG·Qdrant 미가용 시 빈 dict 반환 후 진행.

### D. 워치리스트 안정성 메커니즘이 실제로 작동
MA smoothing(3일 이동평균, `app.py:406-432`) + 히스테리시스(entry 62 / exit 55) 의 이중 안정화.
**실측 증거** (`watchlist_histories`, 2026-04-13~17):
- 하루 7런 × 25종목인데 **하루 distinct 종목 = 30~33개** — 일중 회전이 ~5-8종목뿐
- 일간 watchlist 평균 hybrid 64~76 으로 부드럽게 이동, 급격한 교체 없음

진입/이탈 임계를 분리(62≠55)해 점수가 경계에서 진동해도 종목이 들락거리지 않게 했다.

### E. 섹터 분산이 구조적으로 강제됨
percentile 기반 동적 섹터 budget(HOT/WARM/COOL) + greedy cap. **실측 증거**
(`run_id=scout-20260416-0530`): 25종목이 **11개 섹터에 분산**, 최대 집중 = 4(반도체, cap
override 값과 일치). 단일 섹터 쏠림이 알고리즘적으로 불가능.

### F. 섹터 상대 밸류에이션
절대 PER/PBR 임계 대신 섹터 내 백분위를 우선 사용(`app.py:435-481`, `quant.py:201-214,
249-276`). 성장주 섹터와 가치주 섹터의 밸류 기준 차이를 보정. 섹터 표본 5종목 미만이면
백분위 미적용·절대평가 폴백 — 표본 부족 시 무리하지 않음.

### G. LLM 호출 1-pass 통합
기존 3호출(Hunter+Debate+Judge)을 Unified Analyst 1호출로 통합(`analyst.py` docstring).
종목당 LLM 1회 → 비용·지연·실패표면 1/3. 동시성 20으로 제한해 rate limit 도 관리.

### H. 전 종목 raw 점수 보존 — 별도 rejection 로그를 통합 테이블로 흡수
선정 안 된 종목 포함 전 universe 의 7 서브점수 + LLM 점수를 run_id 별로 `daily_quant_scores`
에 저장하고 `is_final_selected` 플래그로 선정 여부 표시(`app.py:505-551`). 전신이 쓰던 별도
탈락 로그(`shadow_radar_log`, §1.9)를 폐기하고 이 단일 테이블로 흡수 — "무엇을 왜 골랐나/
버렸나"가 한 테이블에서 재구성된다. 실제로 한 달치 20,996행이 남아 사후 회귀·진단이 가능했다
(이 분석의 §2 수치 전부가 그 데이터에서 나옴).

---

## 3. v2가 못한 것 (간략 · 증거)

1. **가중치 합 110 vs 100 캡 — 상위 변별력 손상.** `V2_WEIGHTS` 합 = 20+20+20+10+10+20+10 =
   **110**, 그러나 `total = min(100, ...)` 캡(`quant.py:35, 80-81`). 이론상 100~110 raw 구간이
   전부 100으로 눌려, 최상위 종목들이 동점화. 선정이 점수 정렬 기반이라 최상위 변별력이 깎임.

2. **LLM 이 사실상 무의미했다.** §2-A 의 같은 증거가 약점도 된다: LLM 이 평균 1.1점만 움직이고
   veto 는 1회뿐. 하루 ~880회 LLM 호출(7런 × ~126종목)의 산출 효용이 의문. "LLM 을 썼지만
   거의 안 썼다" — 의도된 보정자였으나, 보정폭이 이 정도면 호출 자체를 둘 가치가 있었는지
   비용 검증이 안 됨. `llm_grade` 는 D 가 67%(14,163행)로 점수와 따로 노는 신호.

3. **죽은 코드 / drift.** `prompts/analyst/unified_analyst.txt` 는 코드 미참조(인라인 프롬프트와
   별도 진화). `QuantScore.matched_conditions` / `condition_win_rate` / `condition_confidence`
   필드(`scoring.py:25-27`) 는 어디서도 채워지지 않는 죽은 필드 — 전신의 `factor_performance`
   실증 승률 시스템(§1.9)의 잔존 훅.

3b. **7팩터 가중치가 라이브 실증 미검증.** `V2_WEIGHTS`(20/20/20/10/10/20/10)·각 팩터의 점수
   버킷 임계값은 전부 손으로 정한 휴리스틱. v2 는 전신이 갖던 조건별 승률 피드백 루프
   (`factor_performance`)를 폐기했고(§1.9), `services/backtest/` 오프라인 엔진은 라이브
   selection 에 연결돼 있지 않음. "이 가중치가 실제로 수익을 냈는가"를 시스템이 스스로
   측정·교정하지 못함. (단 §1.9 — 전신 루프도 표본 n<35 로 약했음)

4. **파이프라인 서브페이즈 누적.** docstring 은 "8단계"지만 실제 Phase 0/1/1.5/2/2.5/2.5b/2.5/
   2.9/3/4/4.5/5/6/6.5/7/8/9 — 유기적으로 덧붙은 흔적. 각 단계는 단순하나 번호 체계가 이미
   포화. (scout 모듈 32커밋의 누적 결과)

5. **하루 7회 재선정의 의도 불명.** universe 가 시총 큰 종목 + 히스테리시스라 결과는 안정적이나
   (§2-D), 장중 매시간 watchlist 를 통째로 갈아끼우는 설계 근거가 코드/주석에 없음. scanner 가
   들고 있는 watchlist 가 계속 교체됨.

6. **universe = 시총순 Top 200 → 소형 성장주 구조적 배제.** `min_market_cap` 500억 +
   `min_price` 1만원 + 시총 내림차순 컷(`universe.py:60-63`). RAG discovery 가 일부 보완하나
   그 편입 효과는 별도 추적 안 됨.

7. **macro 가 selection 을 멈추지 못함.** §1.6 — 약세장에도 scout 는 25종목 watchlist 를
   생성. (방어는 하류 scanner/buyer 의 cash_floor·position_multiplier 에 위임)

---

## 4. v3 비교 훅 (2단계 점검 체크리스트)

2단계(v3 냉정 평가)에서 "v3가 v2의 이 강점을 유지/개선/퇴보시켰나"를 점검할 구체 항목:

1. **결정론/LLM 분리**: v3도 LLM 출력을 ±N clamp 등 코드 layer 로 가두나, 아니면 LLM 이
   점수·선정을 직접 결정하나? v3 LLM 의 실측 영향폭(점수 이동량)을 v2 의 +1.12 와 비교.
2. **스코어링 추적성**: v3 스코어링이 한 파일/한 곳의 가중치로 추적 가능한가, 여러 모듈·서비스·
   stream consumer 에 흩어졌나?
3. **안정성 메커니즘**: v3에 MA smoothing + 히스테리시스(분리된 entry/exit 임계) 등가물이
   있나? v3 buy sheet/watchlist 의 일간 distinct 종목수를 측정 — v2 의 30~33/일(7런)과 비교.
4. **섹터 분산**: v3에 섹터 cap 이 있나? v3 실데이터로 단일 섹터 최대 집중도 측정 — v2 의
   "11섹터 분산, 최대 4" 와 비교.
5. **데이터 부재 견고성**: v3가 종목별·필드별 에러 격리 + 명시적 중립 폴백을 하나, 아니면 한
   종목 실패가 파이프라인/카드를 비우나? (MEMORY `feedback_persist_order_fk` 와 연결)
6. **LLM 효용 대비 비용**: v3 LLM 호출 횟수·비용 산정. v2는 종목당 1-pass(~880/일). v3가 다중
   호출·다단계 council 로 늘었다면, 그 추가 호출이 결정을 실제로 바꾸는지 검증.
7. **raw 점수 보존**: v3가 선정 안 된 종목 포함 전 universe 점수를 run_id 별로 남기나? (사후
   회귀·진단 가능성)
8. **후보→buy sheet 경로 길이**: v2는 quant→LLM→MA→budget→greedy→scanner→buyer(+hard
   floor) — 단계는 많아도 각 단계가 단순·결정론. v3 경로의 단계수 + 각 단계 복잡도(LLM 개입
   여부)를 같은 잣대로 측정.
9. **universe 구성**: v3 universe 가 시총순 컷인가? 소형주 포함 범위가 v2(KOSPI Top 200)보다
   넓/좁은가? RAG/뉴스 기반 발굴의 편입 효과를 v3는 추적하나?
10. **가중치 캡 버그**: v3 스코어링 가중치 합이 만점과 일치하나, v2 의 110/100 미스매치를
    답습했나?
11. **macro gate 성격**: v3 macro/thesis gate 가 hard kill(STOP — 선정 자체 차단)인가 soft
    tune(v2 처럼 RSI·cap 만)인가? v2는 soft 라 약세장에도 watchlist 를 냈다 — v3의 STOP 게이트
    가 측정 윈도우를 통째로 막은 사고(MEMORY `project_thesis_gate_deferred_2026_05_22`)와
    직접 대비. "선정이 멈출 수 있는 구조"가 진화인가 퇴보인가를 이 항목에서 판정.
12. **죽은 코드/문서 drift**: v3에도 미사용 prompt 파일·죽은 스키마 필드가 있나? (v2 패턴 반복
    여부)
13. **실증 피드백 루프**: v3에 선정 가중치·팩터의 라이브 승률 추적 / 탈락 후보 shadow 추적 /
    파라미터 자동 교정이 있나? v2 는 전신의 이 3종(§1.9)을 버리고 손튜닝 휴리스틱으로 갔다 —
    v3가 (a) 그대로 손튜닝인가 (b) 실증 루프를 부활시켰나 (c) 더 후퇴했나. v3 `scout_outcomes_v1`
    /`outcomes` 테이블이 이 용도라면 selection 가중치에 실제로 피드백되는지 확인.

---

## 5. 못 알아낸 것 (정직 고지)

- **v2 MariaDB 접속 완료**(orchestration 에이전트 부활). `daily_quant_scores` 행수 = 20,996 으로
  v3 postgres ETL 분과 **정확히 일치** — §2 의 모든 수치는 v2 의 완전한 데이터셋 기준임.
  단 v2 의 `daily_quant_scores` 자체가 2026-03-18 부터 시작(그 이전 row 없음) — v2 가 quant
  점수 DB 로깅을 03-18 에 켰음. 2026-02-28(council 통합 커밋)~03-17 의 quant 상세는
  v2 DB 에도 없어 미검증.
- **선정→실제 수익 연결**: watchlist 종목이 실제 매수·수익으로 이어진 outcome 은 selection
  영역 밖(execution/orchestration 에이전트). 본 분석은 "무엇을 어떻게 골랐나"까지만.
- **RAG discovery 의 정량 기여**: 코드상 universe 편입은 확인되나, RAG 로 들어온 종목이 최종
  watchlist 에 얼마나 남았는지는 run_id 별 origin 태그가 없어 측정 불가.
- **하루 7회 재선정 설계 의도**: 코드·주석·docstring 어디에도 근거 없음 — 추론만 가능.
