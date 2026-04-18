-- 011_global_macro_news.sql
-- Phase 2.13-1: WSJ/Bloomberg/Reuters RSS 크롤러 + LLM 요약 digest.
--
-- 004_news_sentiments 의 news_articles 는 종목 뉴스(ticker NOT NULL) 전용이라
-- 거시 뉴스를 그대로 담기 어렵다. ticker 없는 전역 매크로 뉴스 + 일일 요약을
-- 별도 스키마로 분리.

-- =====================================================================
-- global_macro_news_articles — 원본 RSS 항목
-- =====================================================================

CREATE TABLE IF NOT EXISTS global_macro_news_articles (
    article_id   TEXT PRIMARY KEY,          -- SHA-256(source_url) 앞 32자
    source       TEXT NOT NULL,              -- 'wsj' | 'bloomberg' | 'reuters' | 'google_news'
    feed_name    TEXT NOT NULL,              -- 세부 RSS 피드 이름 (운영 보기 편하게)
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',   -- RSS description/summary 원본
    source_url   TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_global_macro_news_published
    ON global_macro_news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS ix_global_macro_news_source_published
    ON global_macro_news_articles (source, published_at DESC);


-- =====================================================================
-- global_macro_news_digests — 일일 LLM 요약 (RealWsjDigestFeeder 가 읽음)
-- =====================================================================

CREATE TABLE IF NOT EXISTS global_macro_news_digests (
    digest_date    DATE PRIMARY KEY,
    digest_id      TEXT NOT NULL,            -- 재현 가능한 해시 (raw content SHA-256 앞 16자)
    summary        TEXT NOT NULL,            -- LLM 압축 요약 (또는 fallback formula)
    headlines      TEXT NOT NULL,            -- 불릿 리스트 (source/title/published)
    raw_count      INTEGER NOT NULL,         -- digest 에 포함된 기사 수
    source_counts  JSONB NOT NULL,           -- {"wsj": N, "bloomberg": N, "reuters": N}
    summary_model  TEXT,                     -- 요약에 쓴 모델 (예: "deepseek-chat")
    built_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- 권한
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON global_macro_news_articles, global_macro_news_digests TO pj_runtime;
GRANT SELECT
    ON global_macro_news_articles, global_macro_news_digests TO pj_meta, pj_ui;
