-- migration 021 — stock_masters 에 security_type 컬럼 추가.
--
-- 2026-05-25 사고: scout universe SQL 이 stock_masters 의 is_active + market_cap
-- 만 보고 종목을 가져와서 KODEX 레버리지 + TIGER 미국필라델피아반도체나스닥 같은
-- ETF 가 universe 200 안에 자연 포함됐다. SECTOR_MOMENTUM strategy_tag 로 시트
-- 두 건이 발행됨. ETF/ETN/ELW 는 individual stock 점수 시스템과 맞지 않으므로
-- universe 단계에서 제외해야 한다.
--
-- KIS search-info (CTPF1002R) 응답의 scty_grp_id_cd 를 매핑:
--   ST → STOCK, EF → ETF, EN → ETN, EW → ELW
-- scout/feeders/real.RealUniverseFeeder 는 WHERE security_type = 'STOCK' 추가.
--
-- 기본값 STOCK — 기존 row 는 다음 seed_stock_masters 실행 때 실제 분류로 갱신.
-- universe SQL 은 STOCK 만 보므로 분류 누락 시에도 안전 측 (포함) 으로 동작.

ALTER TABLE stock_masters
    ADD COLUMN IF NOT EXISTS security_type VARCHAR(10) NOT NULL DEFAULT 'STOCK';

CREATE INDEX IF NOT EXISTS ix_stock_masters_security_type
    ON stock_masters (security_type);
