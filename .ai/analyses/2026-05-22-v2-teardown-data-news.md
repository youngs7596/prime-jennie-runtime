# v2 Teardown — 데이터·뉴스 수급 해부

작성: data-news 에이전트 / 2026-05-22 / v2-teardown 팀
대상 코드: `/home/youngs75/projects/prime-jennie` (마지막 커밋 `785641e`, 2026-04-21, 스택 퇴역)
방법: 실제 코드 정독 (file:line 인용) + v2 MariaDB(`jennie_db`, MS-01) 정량 검증 (§5).

---

## 1. v2가 한 일·메커니즘

### 1.1 뉴스 파이프라인 — 3-스레드 stream 구조

`news-pipeline` 서비스(`services/news/app.py`)는 항상 켜진 long-running 서비스로, 한 프로세스 안에서
3개 daemon 스레드를 띄운다 (`app.py:218-224`). Airflow DAG 가 아니라 서비스 내부 루프다.

```
Naver Finance 크롤 ──→ Redis stream:news:raw ──┬──→ Analyzer (group_analyzer)  ──→ LLM 감성 ──→ stock_news_sentiments (DB)
                                               └──→ Archiver (group_archiver)  ──→ 임베딩    ──→ Qdrant rag_stock_data
```

- **Collector** (`collector.py`, `app.py:100-132`): 활성 유니버스(`StockMasterDB.is_active`, 6자리 숫자코드)
  종목별로 네이버 금융 종목뉴스를 크롤(`infra/crawlers/naver.py:crawl_stock_news`), `stream:news:raw`
  에 `xadd` (maxlen 10_000). 장중(07–16시) 10분, 장외 30분 주기 (`app.py:56-86`).
- **Analyzer** (`analyzer.py`): `group_analyzer` consumer group 으로 stream 소비 → 헤드라인 LLM 감성
  분석(score 0–100) → `stock_news_sentiments` 저장 (`analyzer.py:261-290`). `BATCH_SIZE=20` 단위로
  `asyncio.gather` 동시 호출 (`analyzer.py:212-242`).
- **Archiver** (`archiver.py`): `group_archiver` 별도 consumer group 으로 같은 stream 소비 → 임베딩 →
  Qdrant `rag_stock_data` 컬렉션 (LLM 호출 없음, 순수 임베딩).

핵심: **하나의 크롤이 두 소비처(정형 DB + 벡터 DB)에 fan-out** 되고, 두 소비처는 독립
consumer group 이라 한쪽이 죽어도 다른 쪽에 영향이 없다.

### 1.2 중복 제거 — 4겹

1. 크롤러 in-process `_seen_hashes` set — 제목 정규화 해시 (`naver.py:54-61,125-128`).
2. 발행 시점 Redis SET dedup — `dedup:news:YYYYMMDD` 날짜별 키, 3일 TTL, 체크 시 최근 3일 키 모두
   확인 (날짜 경계 중복 방지) (`dedup.py`). `is_new()` 가 체크+마킹 원자적 처리.
3. Analyzer 진입부 PG precheck — `_is_url_exists()` 가 LLM 호출 **전에** `article_url` DB 존재 확인,
   있으면 ack 후 skip (`analyzer.py:147-152, 248-259`).
4. Archiver 별도 dedup SET — `dedup:archive`, 7일 TTL, URL 해시 (`archiver.py:178-195`).

### 1.3 신선도·노이즈 처리

- 크롤 단계 노이즈 필터: `NOISE_KEYWORDS` 18종(특징주/시황/단독/장마감…)을 제목에서 걸러 stream 진입
  자체를 차단 (`naver.py:33-66, 121-122`).
- `published_at` 은 네이버 날짜 파싱; 실패 시 `datetime.now()` 폴백 (`naver.py:139-145`).
- Council 쪽은 프롬프트로 "12시간 이상 오래된 뉴스는 가중치 down" 지시 (`council/pipeline.py:177`).

### 1.4 수집된 데이터가 매매 결정에 닿는 경로 (데이터 흐름 전체)

**경로 A — 정형 감성 (DB):**
`stock_news_sentiments` → `scout/enrichment.py:187-189` 가 `get_news_sentiments(days=14)` 평균을
`news_sentiment_avg` 로 적재 → `scout/quant.py:_news_score()` 가 20–80 점수를 0–10 으로 선형 매핑
(`quant.py:344-352`) → 종목 quant 총점 일부 → 후보 랭킹. 또한 `briefing/reporter.py:217-231` 가
당일 Top-5 감성 뉴스를 일일 브리핑에 표시.

**경로 B — RAG 의미검색 (Qdrant):**
`rag_stock_data` → `scout/rag_retriever.py` 가 두 용도로 사용 —
(1) `discover_rag_candidates()`: 4개 토픽 쿼리(실적개선/수주/신사업·M&A/주주환원)로 **유니버스 밖
신규 후보를 발굴**, 7일 시간필터 적용 (`rag_retriever.py:79-129`).
(2) `fetch_news_for_stocks()`: 종목별 다중쿼리(실적/수주/리스크 + 섹터별 쿼리)로 뉴스 스니펫 수집 →
`EnrichedCandidate.rag_news_context` → `scout/analyst.py:216-218` 가 LLM 애널리스트 프롬프트에 주입.

즉 뉴스는 **(a) quant 팩터 점수 + (b) 후보 발굴 + (c) LLM 컨텍스트** 세 갈래로 선정에 닿는다.

**경로 C — 매크로 council (별도):**
WSJ Gmail 뉴스레터(`crawlers/wsj_gmail.py`) + 네이버 경제·세계 헤드라인(`crawlers/naver_news.py`,
지정학·매크로 키워드 필터) + 텔레그램 브리핑 + 매크로 스냅샷 → 3단계 LLM council
(`council/pipeline.py`: Strategist→Risk Analyst→Chief Judge) → `MacroInsight` →
`_update_trading_context()` 가 regime / `position_multiplier` / `stop_loss_multiplier` /
favor·avoid 섹터로 변환해 `TradingContext` 캐시에 저장 (`jobs/app.py:1886-1941`). 이게 매매 사이징·
손절폭에 직접 닿는다. council DAG: 07:50·11:50 KST 평일 (`dags/macro_dag.py:64-83`).

### 1.5 가격·시장 데이터 수집 (job-worker)

`job-worker` (`services/jobs/app.py`, 2883줄)가 데이터 수집 일꾼. Airflow DAG 가 HTTP 로 트리거
(`dags/utility_jobs_dag.py`). 주요 수집 잡:

| 잡 | 소스 | 대상 | 스케줄 |
|---|---|---|---|
| collect-full-market-data | KIS API | 시총 상위 300 일봉 | 평일 16:00 |
| collect-index-daily-prices | 네이버 fchart | KOSPI/KOSDAQ 지수 | 평일 16:05 |
| collect-us-market | Yahoo Finance | SOX/NVDA/S&P/나스닥선물 | 화–토 07:00 |
| collect-investor-trading | 네이버 금융 | 외인/기관 순매매(300종목) | 평일 18:30 |
| collect-foreign-holding | 네이버 금융 | 외인 지분율(300종목) | 평일 19:00 |
| collect-dart-filings | OpenDartReader | DART 공시 | 평일 18:45 |
| collect-minute-chart | KIS API | 5분봉(상위30+워치리스트) | 장중 5분 |
| collect-consensus | FnGuide 크롤 | Forward PER/EPS/ROE | 월·목 06:00 |
| collect-naver-roe / quarterly-financials | 네이버 금융 | ROE·재무 | 월간·분기 |

실시간 가격: gateway `streamer.py` (KIS WebSocket) → Redis `kis:prices` stream. WebSocket 차단 시
`poller.py` 가 동일 인터페이스(duck typing)로 REST 폴링 3초/15req·s 폴백.

### 1.6 LLM 사용처·비용·안정성

- **news 감성**: `LLMFactory.get_provider("FAST")` → v2 config 상 `ollama` (로컬). **API 비용 0**
  (`config.py:76`, `.env:40`).
- **임베딩**: 로컬 vLLM `KURE-v1` (`config.py:87-88`). 비용 0.
- **macro council**: REASONING/THINKING tier = `deepseek_cloud`, Chief Judge 와 WSJ 요약은 코드에
  `ClaudeLLMProvider` 하드코딩 (`pipeline.py:108-110`, `jobs/app.py:1621-1623`). 회당 ~$0.215
  (DeepSeek×2 + Claude×1, `pipeline.py:8`), 하루 2회 → 월 ~$13 수준.
- **사용량 기록**: 모든 provider 가 `_record_usage()` → Redis `llm:stats:{date}:{service}` 해시,
  35일 TTL, 월별 집계 (`infra/observability/metrics.py`). 단 **토큰만 기록, USD 환산 없음**.
- **안정성**: 감성 LLM 실패 시 score 50(중립) 폴백 (`analyzer.py:233`); council 은
  `_default_risk_analyst` / `_fallback_merge` 로 단계별 폴백 (`pipeline.py:140-152, 308-342`).

---

## 2. v2가 잘한 것 (핵심)

### 2.1 Stream fan-out + 독립 consumer group — 견고한 파이프라인 골격
크롤 1회가 `stream:news:raw` 하나에 발행되고, analyzer/archiver 가 **서로 다른 consumer group**
으로 각자 소비한다 (`analyzer.py:20-22`, `archiver.py:16-19`). 효과:
- 한 크롤로 정형 감성 DB + 벡터 DB 양쪽을 채움 (중복 크롤 없음).
- 한 소비처가 죽어도 다른 쪽 무영향. `xack` + `_process_pending`(id="0" 재읽기)으로 **미ack 메시지
  크래시 복구**가 양쪽 모두 내장 (`analyzer.py:109-134`, `archiver.py:146-176`).
- 소비처 추가가 group 하나 더 만드는 일 — 확장이 싸다.
왜 잘했나: 수집·분석·아카이빙을 시간적으로 decouple 해서, 느린 LLM 분석이 크롤 주기를 막지 않는다.

### 2.2 중복 제거를 4겹으로, 특히 LLM 호출 전 PG precheck
`analyzer._is_url_exists()` 가 **LLM 을 부르기 전에** DB 존재를 확인하고 skip 한다
(`analyzer.py:147-152`). 이건 v3 메모리 `feedback_news_pipeline_pg_precheck` 이 "stream consumer +
외부 LLM 패턴에선 extract 진입부 PG PK 사전체크 필수"라고 학습한 바로 그 패턴 — **v2 는 이미 갖고
있었다.** 거기에 in-process 해시 / 발행 Redis SET(3일, 날짜경계 보정) / archive SET(7일)이 더해져,
재시작·재처리 상황에서 LLM·임베딩 재호출과 DB 중복행을 다층으로 차단한다.

### 2.3 소스 단계 노이즈 필터 — 토큰·저장 낭비를 입구에서 차단
`NOISE_KEYWORDS` 18종을 크롤 단계에서 걸러 stream 진입 자체를 막는다 (`naver.py:33-66`). 시황/특징주/
단독 류 무가치 뉴스는 LLM 도 안 타고 DB 행도 안 만든다. 파이프라인 끝이 아니라 **입구에서** 거른 게
핵심 — 다운스트림 전체의 비용·노이즈가 줄어든다.

### 2.4 비용 의식적 LLM tiering — 고빈도는 로컬, 저빈도만 유료
가장 자주 도는 뉴스 감성을 로컬 Ollama(FAST)로, 임베딩을 로컬 vLLM 로 돌려 **반복 비용을 0** 으로
눌렀다. 유료 API(DeepSeek·Claude)는 하루 2회뿐인 council 에만 썼다. "빈도×단가"를 의식한 배치 —
고빈도 작업을 유료 클라우드에 올리지 않은 절제가 좋다.

### 2.5 contract smoke test — 외부 크롤러 drift 능동 감시
`/jobs/contract-smoke-test` (매일 21:00)가 sentinel 종목 005930 으로 7개 크롤러 계약을 검증한다
(`jobs/app.py:2745-2883`). 단순 None 체크가 아니라 **값 범위 검증**(PER 1–200, ROE −50–100)과
**교차 검증**까지 한다 — 외인+기관+개인 순매매 합 ≈ 0 (`±10000억` 허용), 두 경로로 구한 ROE 차이
≤ 10pp. HTML 구조가 바뀌어 데이터가 조용히 망가지기 전에 잡으려는 설계. 크롤러 의존 시스템에서
드물게 보는 좋은 방어선.

### 2.6 에러 격리 + 우아한 열화
종목별 try/except (`naver.py:165-167`, `collector.py:54-56`), 후보별 try/except
(`enrichment.py` 전 필드), analyzer/archiver 루프의 provider 재생성+백오프
(`app.py:162-165`). 한 종목·한 필드·한 LLM 호출 실패가 파이프라인을 죽이지 않는다. 감성은 실패 시
중립 50, council 은 단계 폴백 — **부분 실패가 trading halt 으로 번지지 않는다.**

### 2.7 운영 현실에 맞춘 부수 설계들
- 장중/장외 차등 주기 (뉴스 10/30분, 매크로 5분).
- 공유 rate limiter — 게이트웨이 19/s 대비 18/s, KIS 20/s 대비 폴러 15/s 로 마진 확보
  (`enrichment.py:203-240`, `jobs/app.py:184`, `poller.py:6`).
- WebSocket 차단 대비 REST 폴러 폴백 (duck-typed 동일 인터페이스).
- 대부분 collect 잡이 DB UPSERT — 재실행 안전(idempotent), 100건 단위 중간 커밋.

---

## 3. v2가 못한 것 (간략·증거)

- **헤드라인-only 감성**: analyzer 가 LLM 에 `headline` 만 보낸다 (`analyzer.py:216-221`). `summary`
  필드는 크롤러가 아예 채우지 않는다 (`naver.py` 에 `summary=` 없음 — grep 확인). 제목 한 줄로
  감성 판정 → 얕다. RAG 아카이브도 `f"[{code}] {headline}"` 만 저장(`archiver.py:220`)이라
  chunk_size=500 splitter 가 무의미.
- **단일 뉴스 소스**: 네이버 금융 종목뉴스 한 곳뿐. `NewsArticle.source` 에 DAUM 이 주석돼 있으나
  미사용. 네이버 HTML 바뀌면 뉴스 수급 전체 정지.
- **DART 공시는 수집만 하고 안 씀**: `stock_disclosures` 적재되지만 `StockDisclosureDB` 소비처가
  jobs 밖에 0건 (grep 확인). 감성·선정 어디에도 안 닿는 dead data.
- **감성 시간가중 없음**: scout 의 `news_sentiment_avg` 는 14일 단순 평균 (`enrichment.py:189`).
  오늘 뉴스와 13일 전 뉴스가 동일 가중. council 프롬프트만 신선도를 말하고 정형 점수는 안 함.
- **`is_emergency` 플래그 dead code**: `_parse_message` 가 긴급 키워드를 계산하지만
  (`analyzer.py:202`) 아무 데서도 fast-track 에 안 쓰임.
- **감성 신뢰도/볼륨 신호 없음**: 기사 1건 평균 80 과 50건 평균 80 이 구별 안 됨. 기사 수가 점수에
  반영 안 됨.
- **`published_at` 파싱 취약**: 네이버 날짜 파싱 실패 시 `datetime.now()` → 잘못된 `news_date` 가
  14일 윈도우·신선도를 오염 (`naver.py:139-145`).
- **poison message 무알림**: LLM 배치가 계속 실패해도 score 50 으로 조용히 저장 — 품질 열화가
  무알림. 크롤러 drift 는 smoke test 가 잡지만 LLM 열화는 감시 공백.
- **크롤러 `_seen_hashes` 무한 증가**: 프로세스 생존 동안 계속 커짐, 테스트에서만 clear
  (`naver.py:383-386`) — long-running collector 의 경미한 누수.
- **감성 점수 강한 긍정 편향** (DB 검증 §5.2): v2-era 16.3만 행 중 sentiment ≥60 이 ~70%,
  <40 은 ~11%, <20 은 단 4건. quant `_news_score` 가 20–80 선형매핑이라 대부분 종목이 6.7–10
  구간에 몰림 → **팩터 변별력이 사실상 죽음**. 부정 뉴스도 30점대로 완만 (예: "삼성 노조 총파업
  20–30조 손실" → 30).
- **source provenance 상실(v2 회귀)**: analyzer 가 `_save_sentiment` 에서 `source="ANALYZER"`
  하드코딩 (`analyzer.py:285`) — 크롤러가 stream 에 실은 `NAVER` 출처가 sentiment 테이블에서
  덮어써짐. 선대(my-prime-jennie) 데이터는 `source=NAVER` 로 출처를 기록했는데(§5.1) v2 가 처리기
  이름으로 바꿔, 다중소스 확장 시 출처 추적 불가.

---

## 4. v3 비교 훅 (2단계 점검 체크리스트)

2단계에서 v3 가 아래 각 항목을 **유지/개선/퇴보** 시켰는지 코드·데이터로 확인할 것.

1. **Stream fan-out 구조**: v3 도 단일 stream → 다중 독립 consumer group 인가? consumer group
   추가만으로 소비처가 늘어나는 구조인가, 아니면 크롤을 반복하는가?
2. **미ack 크래시 복구**: v3 consumer 가 pending(id="0") 재처리를 갖췄나? (메모리
   `feedback_legacy_consumer_groups` — v3 는 옛 group 잔존으로 XLEN 막힌 사고 이력 있음. group
   수명관리가 v2 대비 나아졌나 나빠졌나.)
3. **LLM 호출 전 PG precheck**: v3 news_articles/news_events PK batch 사전체크가 extract 진입부에
   있나? (메모리상 v3 가 이걸 *추가*했다고 학습 — v2 가 이미 갖고 있었다는 사실 대조. v3 가
   "재발견"한 것인지, 더 강화한 것인지 확인.)
4. **다층 dedup**: v3 의 dedup 이 in-process+Redis TTL+PG 4겹을 유지하나? TTL-only 로 후퇴했나?
5. **소스 단계 노이즈 필터**: v3 도 크롤/수집 입구에서 노이즈를 거르나, 아니면 LLM 까지 보낸 뒤
   거르나(토큰 낭비)?
6. **뉴스 소스 다양성**: v3 는 여전히 네이버 단일인가? `global_macro_news_articles`·`news_articles`
   분리가 보이는데 — 소스가 늘었나? (v3 postgres 에 308k news_events / 313k news_articles 존재
   확인 — v2 대비 데이터량은 크게 늘었음. 소스·품질도 늘었는지 별건.)
7. **감성 입력 깊이**: v3 감성 분석이 헤드라인-only 인가, 본문/요약까지 쓰나? (v2 약점 — v3 가
   고쳤는지.)
8. **DART/공시 활용**: v3 `stock_disclosures` 가 감성·선정에 실제로 닿나? (v2 는 dead data.)
9. **감성 시간가중**: v3 가 뉴스 감성에 freshness decay 를 넣었나? (v2 정형 점수 약점.)
10. **LLM tiering·비용**: v3 는 메모리상 Scout/Macro/Briefing/WSJ 전부 DeepSeek
    (`project_llm_tier_assignment`). v2 는 고빈도 뉴스 감성을 **로컬 Ollama(비용 0)** 로 돌렸다 —
    v3 가 이걸 유료 클라우드로 올렸다면 **비용·레이턴시 퇴보**. 뉴스 감성 분석이 v3 에 아직
    있는지, 있다면 어느 tier 인지 확인.
11. **비용 가시성**: v3 가 토큰뿐 아니라 USD 환산 비용을 기록·노출하나? (v2 는 토큰만.)
12. **contract smoke test**: v3 가 외부 크롤러 계약·교차검증 테스트를 유지하나? (v2 의 명백한 강점
    — 퇴보 시 외부 의존 데이터 silent corruption 위험.)
13. **데이터 흐름의 명료성**: v2 는 뉴스→선정 경로가 3갈래(quant 팩터/RAG 발굴/LLM 컨텍스트)로
    명확했다. v3 의 뉴스→선정 경로가 추적 가능한가, 더 단순/복잡해졌나?
14. **rate-limit 마진**: v3 도 게이트웨이/KIS 한도 대비 마진(18/19, 15/20)을 두나?
15. **부분 실패 격리**: v3 도 종목별·필드별 try/except 격리를 유지하나? (메모리 `feedback_*` 에
    silent failure 사고가 여럿 — v3 가 격리를 *약화*시켰을 가능성 점검.)
16. **감성 점수 분포·변별력**: v2 는 ≥60 이 70%로 몰려 뉴스 팩터가 사실상 무변별(§5.2). v3
    `news_sentiments.score` 분포도 같은 편향인가? (메모리 `news_eval_32fix` 가 impact_acc
    60→90.6% 개선을 기록 — v3 가 분포 편향을 실제로 고쳤는지 score 히스토그램으로 확인.)
17. **source provenance**: v3 `news_articles`/`news_sentiments` 가 크롤 출처를 보존하나? (v2 는
    처리기명 'ANALYZER' 로 덮어써 출처 상실 — v3 가 복원했는지.)

---

## 5. 데이터 정량 검증 (v2 MariaDB `jennie_db`)

orchestration 에이전트가 v2 MariaDB 를 부활시켜 §3 약점·§2 강점을 실데이터로 대조.

### 5.1 실사용 테이블 판별
news 감성 테이블 3종이 공존: `stock_news_sentiments`(868,277) / `stock_news_sentiment`(688,830)
/ `news_sentiment`(603,565). **`stock_news_sentiments` 가 v2 실사용본** — v2 코드
`StockNewsSentimentDB.__tablename__` 과 일치하고, news_date 가 2026-04-18(v2 퇴역일)까지 이어짐.
나머지 둘은 2026-02-19 에서 멈춤 → 선대 시스템 잔존. 단·복수 중복 테이블은 선대 마이그레이션 흔적.

### 5.2 v2-era 뉴스 수급 실측 (2026-02-15 ~ 04-18, source=ANALYZER 162,543 행)
- **수집량**: 거래일 ~2,000–7,000 행/일, 종목 100–400/일. 주말 급감(토 460·620, 일 1,224) —
  §2.7 "장중/장외·시장인지 cadence" 가 실제로 작동했음을 확인.
- **`summary` 컬럼 채움율 0%**: v2-era 전 행에서 `summary IS NOT NULL AND <>''` 가 **0건**. §3
  "헤드라인-only" 를 정량 확정 — 2000자 컬럼이 있는데 한 번도 안 씀.
- **감성 분포 강한 긍정 편향**: 60–79 = 103,998(64%), 80–100 = 8,927, 40–59 = 31,100,
  20–39 = 18,514, **00–19 = 4건**. ≥60 이 ~70%. §3 신규 약점(팩터 변별력 사망)의 근거.
- **source 단일·처리기명 고정**: v2-era 162,543 행 전부 `source='ANALYZER'`. 선대 데이터는
  `source='NAVER'`(479,195 행) 이 존재 → v2 가 출처기록을 처리기명으로 회귀시킴(§3).
- LLM 작동 확인: `sentiment_reason` 이 일관된 한국어 — Ollama FAST tier 정상 가동.

### 5.3 기타 테이블 실측
- `stock_disclosures`: 44,797 행, 2026-04-17 까지 수집 지속. **코드상 소비처 0건**(§3) 을 정량
  재확인 — 44k 행을 수집해 한 번도 안 씀.
- `daily_macro_insights`: 43 행, 2026-02-19 ~ 04-17. 날짜당 1행 UPSERT 구조 → council 이 2개월간
  거래일 거의 매일 1회 인사이트 산출. (메모리상 v3 에서 dead 처리된 그 테이블.)
- 가격: `stock_minute_prices`(675,991), `stock_daily_prices_3y`(526,888),
  `stock_investor_trading`(172,627), `us_market_daily`(2,513) — 수집 잡들이 실제로 채워짐.

## 한계 (정직하게)

- v2 크롤 **실패율**은 DB 만으론 직접 측정 불가(성공분만 적재) — §2.6 에러격리는 코드 기반 결론.
- §1.6 비용 추정(월 ~$13)은 council 회당 $0.215(코드 주석값)×하루 2회 산술 — 실제 청구서 미확인.
- v3 postgres 의 news 테이블은 v3 네이티브 스키마(`ticker/article_id/score/label`) — v2 ETL
  잔존분 아님. v2/v3 데이터 비교는 2단계에서 양쪽 DB 를 직접 대조해야 함.
