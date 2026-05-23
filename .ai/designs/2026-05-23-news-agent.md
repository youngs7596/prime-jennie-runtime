# News Agent — design (2026-05-23)

> 한국 금융 뉴스 처리 파이프라인을 단순 metadata extractor 에서 **prime-jennie v3
> 전용 LLM Agent** 로 진화. L2 (인지 + 종합) 까지 책임. minyoung-mah SubAgentRole
> 패턴. Scout 의 결정론 코어와 분리된 LLM-at-core 영역.

## 1. Why

### 1.1 현재 false-positive (2026-05-23 실측)

`news_articles` 한 article_id 가 다수 ticker 로 중복 저장됨:

- 같은 SK온 기사 (`article_id=0000047401&office_id=648`) 가 7개 ticker 로 저장:
  009830 한화솔루션 / 010120 LS / 034730 SK / 066570 LG전자 / 096770 SK이노 /
  267250 HD현대 / 267270 HD건설기계
- 같은 삼성물산 압구정4구역 수주 기사가 028260 삼성물산 + 055550 신한지주 양쪽

Root cause:

1. 네이버 금융이 종목별 뉴스 페이지에 시장/계열 뉴스를 함께 큐레이션
2. crawler 가 ticker 별로 별도 페이지 (`finance.naver.com/item/news_news.naver?code={ticker}`) 를 긁어 결과 article 의 `ticker` 필드에 그 ticker 를 강제 부여
3. dedup fingerprint = `md5(source_url)` 인데 URL 에 `code={ticker}` 가 박혀 ticker 마다 다른 fingerprint → 모두 dedup 통과
4. LLM extractor 는 prompt 에 ticker 를 input 으로 받기만 하고 검증 안 함. ticker 결정 권한 0

결과: false positive 다수, Scout 가 false ticker 로 라벨된 뉴스를 그 종목의 점수에 반영.

### 1.2 본문 발췌 한계

`exaone_extractor.py:159` 의 `body_excerpt_chars: int = 400`. 풀 본문 (~2000자) 이
`news_articles.body` 에 저장되지만 LLM prompt 입력은 첫 400자만. 의도는 prompt 길이
절감인데 사용자 의도와 어긋남.

### 1.3 비용 제약 부재

EXAONE → Qwen3-30B-A3B 로 전환된 vLLM 이 MS-01 eGPU (RTX 3090) 로컬 운영.
외부 API 비용 0. throughput 만 제약. 따라서 **정확도 < 토큰 절약 안 됨** — 풀
본문 + 다단계 추출 + 재검증 자유.

## 2. Agent 명세

### 2.1 역할 (L2)

| 책임 | In scope |
|---|---|
| **인지** | 기사 → metadata + **진짜 관련 ticker(s)** 결정. 0..N (0=일반 시장 뉴스) |
| **종합** | ticker 별 컨텍스트 의미 부여. 예: "이 뉴스는 069500 보유 중인 macro gate=open 상황에서 medium downside" |
| 시간성 | "따끈따끈한 알림" 아님. **누적 이력**. Scout 가 시간 윈도우(예: 24h) 묶어 소비 |

Out of scope:

- L3 (행동 제안) — 별도 design
- L4 (자율 집행) — [feedback_no_reflex_stop] / [feedback_prompt_control_limit] 학습 충돌

### 2.2 입출력

**입력**:
- 단일 `NewsArticle` (title + 풀 body + source_url + published_at + source crawler 가
  부여한 candidate ticker)
- 종목 universe (stock_masters 의 `stock_code`, `stock_name`) — Agent 가 ticker 결정에 활용

**출력 (확장된 `NewsEvent`)**:
```
event_type, impact_level, sentiment, sentiment_score, time_horizon,
keywords, sector_tags, financial_signals, confidence,         # 기존
tickers: list[str],            # NEW. LLM 이 본문 기반 판단한 진짜 관련 ticker 0..N
ticker_rationales: dict[ticker, str]  # NEW. 종목별 1~2문장 이유 (감사 트레일)
```

`article.ticker` (crawler 가 부여한 entry point) 는 NewsEvent 에 안 들어감. tickers
가 empty 면 일반 시장 뉴스로 (event_type=market_movement 등) 저장.

### 2.3 모델

- 운영: **Qwen3-30B-A3B-Instruct-2507-Autoround-Int-4bit-gptq** (vLLM, MS-01 eGPU)
- 토큰 비용 0. prompt 풀 본문 (~2000자) + universe context 자유롭게 사용
- throughput 제약은 vLLM 의 max-concurrency (현재 4.96x @ 4096 ctx). Agent 호출
  병렬도는 이 한도 이하 유지

### 2.4 Harness

- minyoung-mah `SubAgentRole` 로 정착. Scout/Macro 와 동일 패턴
- role name: `news_agent`
- `RoleInvocationResult.metadata` 에 usage(token I/O) 기록 (LLM 통계 UI 호환)

## 3. 소스 Adapter

### 3.1 Protocol

```python
class NewsSource(Protocol):
    async def fetch(self, *, since: datetime, universe_hint: list[str]) -> list[NewsArticle]: ...
```

- `since`: 마지막 폴 시각 이후 신규 기사만
- `universe_hint`: 종목 universe (구체적 사용은 source 자율)
- 결과 `NewsArticle.ticker` 는 source 가 "발견 entry point" 로만 부여. Agent 가 무시
  하고 자체 판단

### 3.2 Phase 1 source

- `NaverStockNewsSource` (기존 `NaverNewsCrawler` 의 풀 본문 fetch 사용)
- `NaverMarketNewsSource` (네이버 금융 일반 뉴스 — 종목 referrer 무관)

### 3.3 향후 (out of scope, Phase 2+)

- 매경/한경 RSS, 인포스탁, x.com (검색 가능 시), 영문 (Bloomberg/Reuters via 다른 agent)

## 4. Persistence

### 4.1 현재 schema 의 문제

- `news_articles.ticker` PK 일부 (article_id, ticker 또는 article_id 단일)
- `news_events.ticker` 단일 컬럼

→ 한 기사를 여러 진짜 ticker 와 매핑하려면 schema 변경 필수.

### 4.2 신규 schema

```sql
-- 기존 news_articles.ticker 컬럼은 backward-compat 위해 "discovered_via_ticker"
-- 의미로 남김 (entry point 추적). 신규 진짜 ticker 관계는 별도 테이블.

CREATE TABLE news_event_tickers (
    article_id  text REFERENCES news_articles(article_id) ON DELETE CASCADE,
    ticker      text NOT NULL,
    rationale   text,
    confidence  numeric(3,2),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, ticker)
);

CREATE INDEX news_event_tickers_ticker_recent_idx
    ON news_event_tickers (ticker, created_at DESC);
```

- `news_events.ticker` 컬럼은 deprecate (Phase 2 backfill 후 drop)
- Scout feeder 의 `recent_for_ticker(ticker)` 는 `news_event_tickers JOIN news_events`
  로 재구현

### 4.3 Backfill

- 기존 news_articles 132건의 outcomes / scout_runs 영향 받는 기간은 backfill 대상.
  단 dedup 약점으로 다수 false ticker 가 이미 저장됨 → backfill 시 Agent 재실행이
  가장 깔끔. 대규모 일감이라 별도 task 로 분리 (Phase 2 진입 직전)

## 5. Pre-flight (gate, 사람 라벨 없음)

### 5.1 한계 인지

사람 ground truth 없이는 정확한 precision/recall 측정 불가. 본 protocol 은
**proxy metric** 으로 false positive 차단 효과를 정량화. proxy 의 노이즈는
±5%p 수준 — gate 통과 = "false positive 가 대부분 제거됐다" 의 약한 보장.

진짜 검증은 Phase 1 shadow 운영의 일별 disagreement 사례를 사용자가 spot-check
하면서 자연스럽게 이뤄짐.

### 5.2 샘플

- 50건: 최근 5 거래일 `news_articles` 무작위 추출. event_type 분포가 한 종류에
  쏠리지 않게 stratified random (event_type 분류된 news_events 기준)
- 10건: 알려진 false positive cluster 강제 포함. SQL 로 `article_id` 가 N(≥3) 개
  ticker 와 매핑된 row 들 중 상위 N 케이스 sampling. SK온 cluster / 삼성물산
  cluster / 압구정 cluster 포함

### 5.3 Agent (Pre-flight 전용 minimal)

정식 Agent 구현 전이라 Pre-flight 는 다음으로 대체:

- 기존 `LiteLLMEventExtractor` 의 prompt 를 확장한 사본 (스크립트 내부)
- 신규 출력 필드: `tickers: list[str]`, `ticker_rationales: dict[str, str]`
- prompt 에 `종목코드: {ticker}` 라인 제거 → LLM 입력 bias 차단
- prompt 에 universe 가이드: "한국 상장사 종목명/별칭을 인지하여 tickers 에 매핑"

Pre-flight 가 통과되면 본 변경을 정식 Agent 로 승격.

### 5.4 Metric

| # | 이름 | 정의 | 목표 |
|---|---|---|---|
| 1 | **Title-coverage rate** | Agent 의 tickers 중 stock_name 또는 별칭이 title+body 에 substring 등장하는 비율 | ≥ 0.95 |
| 2 | **Cluster reduction** | 알려진 FP cluster 10건의 Agent 출력 ticker 수 평균 | ≤ 1.5 (원 cluster 평균 ~5) |
| 3 | **Cross-LLM sanity** (10건 한정) | Agent (Qwen3) tickers ∩ Claude Sonnet (또는 DeepSeek) tickers / union | ≥ 0.80 |
| 4 | **Crawler overlap** (information only) | Agent tickers ∩ crawler ticker 의 비율. 낮을수록 Agent 가 기존과 다른 판단 | gate 아님, 기록만 |

### 5.5 Gate 결정

- Metric 1 ≥ 0.95 **AND** Metric 2 ≤ 1.5 → Phase 1 (shadow) 진입
- Metric 3 < 0.80 시 prompt 개선 후 재실행 (gate 통과 못해도 Phase 1 진입은 1·2 만으로 결정)
- Metric 4 는 정보 — Phase 1 shadow 일별 disagreement 분석의 초기값

### 5.6 산출물

`.ai/analyses/2026-05-23-news-agent-preflight.md` — 60 sample 라벨링 결과, 4
metric 값, gate 판정, 대표 disagreement 사례 5건 분석. 추후 회귀 baseline.

## 6. Phase

### Phase 1 — Shadow (gate 통과 후)

- Agent 가 매 기사에 대해 실행. `tickers`/`ticker_rationales` 를
  `news_events.metadata_json.shadow_tickers` 에 기록만
- 기존 path (crawler ticker 그대로 저장) 와 병행. 운영 영향 0
- Scout feeder 는 기존 path 사용 유지
- Phase 1 기간: 5 거래일 (~ 2026-05-30). 새 false positive 발견 / Agent 누락 분석
- Phase 1 산출물: `.ai/analyses/2026-05-XX-news-agent-shadow.md` — 일별 일치율,
  대표 disagreement 사례 5건 분석

### Phase 2 — 정식 전환

조건: Phase 1 일치율 ≥ 95% 또는 disagreement 가 모두 Agent 가 맞고 crawler 가
틀린 경우.

- 신규 schema 마이그레이션 (`news_event_tickers` 테이블 + 인덱스)
- 신규 NewsAgent 가 분석한 결과를 `news_event_tickers` 에 정식 저장
- Scout feeder 신규 schema 로 전환
- 기존 path dead 처리 (`news_events.ticker` 컬럼 일단 NULL 허용으로 두고,
  운영 안정 1주 후 DROP)
- backfill: 최근 30일 news_articles 재실행 (vLLM 로컬이라 비용 0, throughput 만 부담)

## 7. Out of scope (확장 path 만 명시, 본 design 에서 구현 안 함)

- L3 행동 제안 (watchlist 편입/리스크 알림)
- L4 자율 집행
- 비-네이버 소스 (RSS / x.com / 영문) 의 실제 adapter 구현
- LLM-at-core 의 결정론 fallback (현 vision 은 LLM 단독)
- Agent 출력의 사람-가독 UI (control-ui 의 새 페이지) — Phase 2 후

## 8. Decision log

| 결정 | 사유 |
|---|---|
| L2 까지 (L3/L4 보류) | [feedback_no_reflex_stop] / [feedback_prompt_control_limit]
  학습. 결정론 코어 (Scout 의 quant.py) 와 LLM advisory 의 분리 유지 |
| LLM-at-core | 자연어 입력 + entity 결정이라 결정론 layer 비효율. Scout 의
  selection 결정과는 책임 분리. [project_selection_architecture_decision] 의
  "LLM-at-core 폐기" 는 selection 전용이지 news 적용 안 됨 |
| 풀 본문 사용 | Qwen3-30B 로컬 운영, 토큰 비용 0. 정확도 최우선 |
| Pre-flight 50+10 샘플 | [feedback_single_day_overfit] — n<30 single-day 금지.
  50건 + 강제 false positive 10건으로 outcome 구분 측정 |
| article ↔ ticker 다대다 | 한 기사 → 진짜 N ticker (정상 케이스. 예: 두 회사
  합병 뉴스) 와 일반 시장 뉴스 (ticker=0) 를 자연스럽게 표현 |
| harness role | Scout/Macro 와 동일 minyoung-mah 패턴 → 운영/모니터링 일관성
  (LLM stats UI, persistence path, cost_est 등 재사용) |

## 9. 차기 action

본 design 승인 시:

1. **Pre-flight 실행** — `scripts/news_agent_preflight.py` 작성, 50+10 샘플 라벨,
   측정 결과 산출 → analyses doc
2. Gate 통과 시 Phase 1 (shadow) 구현 — 기존 pipeline 에 Agent 추가 호출 path,
   shadow_tickers 기록
3. (Phase 1 5거래일 후) 정량 평가 → Phase 2 진입 결정

Phase 1 시작 전 별도 quick fix 후보: `body_excerpt_chars` 400 → 본문 전체 (즉시
1줄 변경. design 별개로 사용자 승인 시 선행 가능)
