# news_pipeline_global

전역 매크로 뉴스 (WSJ/Bloomberg/Reuters) RSS 수집 + 일일 LLM 요약.

## 책임 경계

- **긁는다**: RSS 피드 (paywall 뒤 본문은 수집하지 않음 — title + description 만)
- **저장**: `global_macro_news_articles` (원본), `global_macro_news_digests` (일일 요약)
- **읽는 쪽**: `slow_loop.macro.feeders.real.RealWsjDigestFeeder`

종목별 국내 뉴스는 `news_pipeline_kor/` 소관. 두 파이프라인은 스키마도 목적도 달라서 공유하지 않는다.

## 흐름

```
crawl_cycle (2h 주기)   build_digest (07:30, 11:30 평일)
    ↓                         ↓
  RSS → articles 테이블 ── 최근 24h → LLM 요약 → digests 테이블
                                                      ↓
                                     Macro Council 이 읽어감
```

## RSS 피드

`feeds.py` 의 `DEFAULT_FEEDS`. env `GLOBAL_NEWS_FEEDS_JSON` 으로 전면 교체 가능.

- WSJ 는 공식 RSS (paywall 은 본문에만 — description 은 공개)
- Bloomberg 는 `feeds.bloomberg.com/{markets,economics}/news.rss`
- Reuters 공식 RSS 는 폐지 → Google News RSS 경유 (`site:reuters.com`)

## LLM 요약

- 모델: DeepSeek chat V3.2 (`deepseek-chat`) — 영문 매크로 → 한국어 6~10줄
- API 키 없으면 headlines 포뮬러 fallback (Macro 가 unavailable 보다 낫다)
- 호출 비용 ~$0.001/회. 일 2회 × 평일 = 월 ~40회 → 월 $0.04

## 실패 모드

- RSS 피드 1개 실패 → 그 피드만 skip. 나머지 집계 진행
- LLM 요약 실패 → fallback summary, `summary_model=NULL`
- digest 테이블 비어 있음 → `RealWsjDigestFeeder` 가 Phase 2.12 factual fallback (us_market_daily)

## 스키마

migration 011. articles.article_id = SHA256(source_url)[:32]. digests.digest_date PK.
