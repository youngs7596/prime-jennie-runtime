-- 004_news_sentiments.sql
-- v3 뉴스 감성 분석 결과 (Phase 2.7).
-- 1 기사 → 1 (ticker, article_id) row. 재분석 시 덮어쓰기.

CREATE TABLE IF NOT EXISTS news_sentiments (
    ticker         TEXT NOT NULL,
    article_id     TEXT NOT NULL,
    score          DOUBLE PRECISION NOT NULL,
    label          TEXT NOT NULL,          -- positive | neutral | negative
    analyzed_at    TIMESTAMPTZ NOT NULL,
    model          TEXT NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, article_id)
);

CREATE INDEX IF NOT EXISTS ix_news_sentiments_ticker_analyzed
    ON news_sentiments (ticker, analyzed_at DESC);


-- 기사 메타 (크롤러 원본) — 별도 테이블. upsert(score, article=) 호출 시에만 저장.
CREATE TABLE IF NOT EXISTS news_articles (
    article_id      TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    published_at    TIMESTAMPTZ NOT NULL,
    source_url      TEXT NOT NULL,
    source_name     TEXT NOT NULL DEFAULT '',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_news_articles_ticker_published
    ON news_articles (ticker, published_at DESC);


GRANT SELECT, INSERT, UPDATE, DELETE
    ON news_sentiments, news_articles TO pj_runtime;
GRANT SELECT ON news_sentiments, news_articles TO pj_meta, pj_ui;
