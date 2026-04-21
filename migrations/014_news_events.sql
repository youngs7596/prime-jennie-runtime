-- 014_news_events.sql
-- Track E — EXAONE 감성점수 대신 "메타데이터 추출" 로 전환.
--
-- 벡터 DB (Qdrant v3_news_sentiments) 는 국내 금융 뉴스의 semantic duplicate
-- 특성상 top-k 가 "동일 사건의 여러 매체 인용" 으로 채워져 효용이 낮아, 메타데이터
-- 기반 RDB 조건식으로 전환. Scout (Opus) 이 SQL/pandas 식 필터링을 더 잘 다룸.
--
-- 기존 news_sentiments 테이블은 이력으로 유지하되 write 는 중단. news_articles 는
-- 그대로 재사용 (article_id FK).

CREATE TABLE IF NOT EXISTS news_events (
    article_id       TEXT        PRIMARY KEY REFERENCES news_articles(article_id)
                                                ON DELETE CASCADE,
    ticker           VARCHAR(10) NOT NULL,
    published_at     TIMESTAMPTZ NOT NULL,

    -- 분류
    event_type       VARCHAR(30) NOT NULL,   -- earnings/mna/lawsuit/product/personnel/
                                              -- regulation/contract/strike/other 등
    impact_level     VARCHAR(10) NOT NULL,   -- high/medium/low
    sentiment        VARCHAR(10) NOT NULL,   -- positive/neutral/negative
    sentiment_score  NUMERIC(4, 3),          -- -1.000 ~ +1.000. Scout 호환용 보조.
    time_horizon     VARCHAR(10),            -- immediate/short/medium/long

    -- 구조화 추출
    keywords         TEXT[],                 -- 상위 3~5개
    sector_tags      TEXT[],
    financial_signals JSONB,                 -- [{"type":"revenue","direction":"up"},...]
    confidence       NUMERIC(3, 2),          -- 0.00 ~ 1.00

    -- 메타
    model            VARCHAR(80)             NOT NULL,
    analyzed_at      TIMESTAMPTZ             NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ             NOT NULL DEFAULT NOW()
);

-- Scout feeder 는 ticker × 최근 N시간 윈도우 조회. published_at DESC 정렬 필수.
CREATE INDEX IF NOT EXISTS ix_news_events_ticker_published
    ON news_events (ticker, published_at DESC);

-- Macro/Scout prompt 에서 event_type 필터 자주 사용 — 예: earnings 최근 24h.
CREATE INDEX IF NOT EXISTS ix_news_events_event_type
    ON news_events (event_type, published_at DESC);

-- high-impact 만 선행 조회하는 쿼리 최적화 (부분 인덱스).
CREATE INDEX IF NOT EXISTS ix_news_events_high_impact
    ON news_events (ticker, published_at DESC)
    WHERE impact_level = 'high';


-- =====================================================================
-- 권한 (001 전역 정책과 동일)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON news_events TO pj_runtime;
GRANT SELECT ON news_events TO pj_meta, pj_ui;
