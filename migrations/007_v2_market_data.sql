-- 007_v2_market_data.sql
-- v2 포팅: 시장 데이터 라이브 테이블 6종.
-- 원본:
--   prime-jennie/migrations/versions/001_initial_schema.py (stock_investor_tradings, stock_fundamentals)
--   prime-jennie/migrations/versions/003_add_disclosures_and_minute_prices.py (stock_disclosures)
--   prime-jennie/migrations/versions/004_fix_stock_disclosures_legacy_columns.py (컬럼명 확정)
--   prime-jennie/migrations/versions/007_add_stock_consensus_table.py (stock_consensus)
--   prime-jennie/migrations/versions/009_add_index_daily_prices.py (index_daily_prices)
--   prime-jennie/migrations/versions/011_add_us_market_daily.py (us_market_daily)

-- =====================================================================
-- stock_investor_tradings — 일별 외국인/기관/개인 순매수
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_investor_tradings (
    id                      BIGSERIAL PRIMARY KEY,
    stock_code              VARCHAR(10) NOT NULL REFERENCES stock_masters(stock_code),
    trade_date              DATE NOT NULL,
    foreign_net_buy         DOUBLE PRECISION,
    institution_net_buy     DOUBLE PRECISION,
    individual_net_buy      DOUBLE PRECISION,
    foreign_holding_ratio   DOUBLE PRECISION,
    CONSTRAINT uq_investor_code_date UNIQUE (stock_code, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_investor_date
    ON stock_investor_tradings (trade_date, stock_code);


-- =====================================================================
-- stock_fundamentals — 일별 PER/PBR/ROE + 시총
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_fundamentals (
    stock_code      VARCHAR(10) NOT NULL REFERENCES stock_masters(stock_code),
    trade_date      DATE NOT NULL,
    per             DOUBLE PRECISION,
    pbr             DOUBLE PRECISION,
    roe             DOUBLE PRECISION,
    market_cap      BIGINT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (stock_code, trade_date)
);


-- =====================================================================
-- stock_disclosures — DART 공시 (004 컬럼명 확정본: report_type, corp_name)
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_disclosures (
    id                  BIGSERIAL PRIMARY KEY,
    stock_code          VARCHAR(10) NOT NULL REFERENCES stock_masters(stock_code),
    disclosure_date     DATE NOT NULL,
    title               VARCHAR(500) NOT NULL,
    report_type         VARCHAR(50),
    receipt_no          VARCHAR(20) NOT NULL,
    corp_name           VARCHAR(100),
    CONSTRAINT uq_disclosure_receipt UNIQUE (receipt_no)
);

CREATE INDEX IF NOT EXISTS ix_disclosure_code_date
    ON stock_disclosures (stock_code, disclosure_date);


-- =====================================================================
-- stock_consensus — Forward PER/EPS/ROE + 목표주가 (FNGUIDE 등)
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_consensus (
    stock_code          VARCHAR(10) NOT NULL REFERENCES stock_masters(stock_code),
    trade_date          DATE NOT NULL,
    forward_per         DOUBLE PRECISION,
    forward_eps         DOUBLE PRECISION,
    forward_roe         DOUBLE PRECISION,
    target_price        INTEGER,
    analyst_count       INTEGER,
    investment_opinion  DOUBLE PRECISION,
    source              VARCHAR(20) NOT NULL DEFAULT 'FNGUIDE',
    PRIMARY KEY (stock_code, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_consensus_date
    ON stock_consensus (trade_date);


-- =====================================================================
-- index_daily_prices — KOSPI/KOSDAQ 일봉 OHLCV
-- =====================================================================

CREATE TABLE IF NOT EXISTS index_daily_prices (
    index_code      VARCHAR(10) NOT NULL,
    price_date      DATE NOT NULL,
    open_price      DOUBLE PRECISION NOT NULL,
    high_price      DOUBLE PRECISION NOT NULL,
    low_price       DOUBLE PRECISION NOT NULL,
    close_price     DOUBLE PRECISION NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    change_pct      DOUBLE PRECISION,
    PRIMARY KEY (index_code, price_date)
);

CREATE INDEX IF NOT EXISTS ix_index_daily_date
    ON index_daily_prices (price_date);


-- =====================================================================
-- us_market_daily — 미국 지표 (SOX, NVDA, S&P 500, 나스닥 선물)
-- =====================================================================

CREATE TABLE IF NOT EXISTS us_market_daily (
    ticker          VARCHAR(10) NOT NULL,
    price_date      DATE NOT NULL,
    open_price      DOUBLE PRECISION NOT NULL,
    high_price      DOUBLE PRECISION NOT NULL,
    low_price       DOUBLE PRECISION NOT NULL,
    close_price     DOUBLE PRECISION NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    change_pct      DOUBLE PRECISION,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, price_date)
);

CREATE INDEX IF NOT EXISTS ix_us_market_daily_date
    ON us_market_daily (price_date);


-- =====================================================================
-- 권한 (001 전역 정책과 동일)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON stock_investor_tradings, stock_fundamentals, stock_disclosures,
       stock_consensus, index_daily_prices, us_market_daily
    TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE stock_investor_tradings_id_seq TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE stock_disclosures_id_seq TO pj_runtime;
GRANT SELECT
    ON stock_investor_tradings, stock_fundamentals, stock_disclosures,
       stock_consensus, index_daily_prices, us_market_daily
    TO pj_meta, pj_ui;
