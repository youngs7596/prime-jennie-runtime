-- migration 020 — news_events 에 shadow_metadata jsonb 컬럼 추가.
--
-- Phase 1 (shadow) News Agent 출력 (tickers/rationales/is_market_general/confidence)
-- 을 운영 영향 없이 기록. design `.ai/designs/2026-05-23-news-agent.md` §6 Phase 1.
--
-- Phase 2 (정식 전환) 진입 시 별도 `news_event_tickers` 테이블이 생기고 본 컬럼은
-- 분석/감사용 trail 로 남음.

ALTER TABLE news_events
    ADD COLUMN IF NOT EXISTS shadow_metadata JSONB;
