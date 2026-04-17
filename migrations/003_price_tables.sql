-- 003_price_tables.sql
-- v3 자체 시세 누적 테이블 (Phase 2 D2).
-- KIS API 응답을 read-through 캐시로 누적 → fallback 조회 소스.
-- v2 `stock_minute_prices` / `stock_daily_prices` 는 read-only 마운트로 분리 (마이그 없음).

-- =====================================================================
-- daily_prices — 일봉 OHLCV
-- =====================================================================

CREATE TABLE IF NOT EXISTS daily_prices (
    stock_code      TEXT NOT NULL,
    price_date      DATE NOT NULL,
    open_price      INT NOT NULL,
    high_price      INT NOT NULL,
    low_price       INT NOT NULL,
    close_price     INT NOT NULL,
    volume          BIGINT NOT NULL,
    change_pct      DOUBLE PRECISION,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (stock_code, price_date)
);

CREATE INDEX IF NOT EXISTS ix_daily_prices_date ON daily_prices (price_date);


-- =====================================================================
-- minute_prices — 분봉 OHLCV
-- =====================================================================

CREATE TABLE IF NOT EXISTS minute_prices (
    stock_code       TEXT NOT NULL,
    price_datetime   TIMESTAMPTZ NOT NULL,
    open_price       INT NOT NULL,
    high_price       INT NOT NULL,
    low_price        INT NOT NULL,
    close_price      INT NOT NULL,
    volume           BIGINT NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (stock_code, price_datetime)
);

CREATE INDEX IF NOT EXISTS ix_minute_prices_datetime
    ON minute_prices (price_datetime);


-- =====================================================================
-- 권한 (001 전역 정책과 동일)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON daily_prices, minute_prices TO pj_runtime;
GRANT SELECT ON daily_prices, minute_prices TO pj_meta, pj_ui;
