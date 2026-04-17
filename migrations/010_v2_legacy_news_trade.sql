-- 010_v2_legacy_news_trade.sql
-- v2 포팅: 아카이브 legacy_* 테이블 2종.
-- 원본:
--   prime-jennie/migrations/versions/001_initial_schema.py (stock_news_sentiments, trade_logs, signal_logs 의 v2 운영 필드)
-- 설계:
--   v3 운영 news 파이프라인은 news_articles + news_sentiments (004_news_sentiments.sql) 를 씀.
--   v2 stock_news_sentiments 는 구조 (press/headline/summary/sentiment_reason/...) 가 다르므로
--   컷오버 아카이브용 legacy_news_sentiments 를 별도 테이블로 보존.
--   legacy_signal_logs, legacy_trade_logs, legacy_quant_scores 는 이미 002_legacy_namespace.sql 에 있음.

-- =====================================================================
-- legacy_news_sentiments — v2 stock_news_sentiments 아카이브 (1:1 복사)
-- =====================================================================

CREATE TABLE IF NOT EXISTS legacy_news_sentiments (
    id                  BIGINT PRIMARY KEY,
    stock_code          VARCHAR(10) NOT NULL,
    news_date           DATE NOT NULL,
    press               VARCHAR(100),
    headline            VARCHAR(500) NOT NULL,
    summary             VARCHAR(2000),
    sentiment_score     DOUBLE PRECISION NOT NULL,
    sentiment_reason    VARCHAR(2000),
    category            VARCHAR(50),
    article_url         VARCHAR(1000) NOT NULL,
    published_at        TIMESTAMPTZ,
    source              VARCHAR(20),
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_legacy_news_url UNIQUE (article_url)
);

CREATE INDEX IF NOT EXISTS ix_legacy_news_code_date
    ON legacy_news_sentiments (stock_code, news_date);


-- =====================================================================
-- 권한 (002 와 동일 패턴)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON legacy_news_sentiments TO pj_runtime;
GRANT SELECT
    ON legacy_news_sentiments TO pj_meta, pj_ui;
