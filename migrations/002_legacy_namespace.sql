-- 002_legacy_namespace.sql
-- v2 MariaDB → v3 Postgres 일방 마이그레이션용 legacy_* 테이블.
-- v2 스키마 그대로 복사 (재해석 없음). 컬럼명/타입은 v2와 1:1.
-- Phase 0 design §13.2.

-- =====================================================================
-- legacy_signal_logs — v2 signal_logs
-- =====================================================================

CREATE TABLE IF NOT EXISTS legacy_signal_logs (
    id                   BIGINT PRIMARY KEY,
    signal_type          TEXT NOT NULL,
    stock_code           TEXT NOT NULL,
    stock_name           TEXT NOT NULL,
    strategy             TEXT,
    price                INT NOT NULL,
    quantity             INT,
    hybrid_score         DOUBLE PRECISION,
    rsi_value            DOUBLE PRECISION,
    volume_ratio         DOUBLE PRECISION,
    market_regime        TEXT,
    position_multiplier  DOUBLE PRECISION,
    profit_pct           DOUBLE PRECISION,
    holding_days         INT,
    status               TEXT NOT NULL,
    suppressed_reason    TEXT,
    created_at           TIMESTAMPTZ NOT NULL,
    imported_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_legacy_signal_logs_type_time
    ON legacy_signal_logs (signal_type, created_at);
CREATE INDEX IF NOT EXISTS ix_legacy_signal_logs_code
    ON legacy_signal_logs (stock_code, created_at);


-- =====================================================================
-- legacy_trade_logs — v2 trade_logs
-- =====================================================================

CREATE TABLE IF NOT EXISTS legacy_trade_logs (
    id                 BIGINT PRIMARY KEY,
    stock_code         TEXT NOT NULL,
    stock_name         TEXT NOT NULL,
    trade_type         TEXT NOT NULL,
    quantity           INT NOT NULL,
    price              INT NOT NULL,
    total_amount       BIGINT NOT NULL,
    reason             TEXT NOT NULL,
    strategy_signal    TEXT,
    market_regime      TEXT,
    llm_score          DOUBLE PRECISION,
    hybrid_score       DOUBLE PRECISION,
    trade_tier         TEXT,
    profit_pct         DOUBLE PRECISION,
    profit_amount      BIGINT,
    holding_days       INT,
    trade_timestamp    TIMESTAMPTZ NOT NULL,
    imported_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_legacy_trade_logs_code_time
    ON legacy_trade_logs (stock_code, trade_timestamp);
CREATE INDEX IF NOT EXISTS ix_legacy_trade_logs_type_time
    ON legacy_trade_logs (trade_type, trade_timestamp);


-- =====================================================================
-- legacy_quant_scores — v2 daily_quant_scores
-- =====================================================================

CREATE TABLE IF NOT EXISTS legacy_quant_scores (
    id                    BIGINT PRIMARY KEY,
    score_date            DATE NOT NULL,
    stock_code            TEXT NOT NULL,
    stock_name            TEXT NOT NULL,
    total_quant_score     DOUBLE PRECISION NOT NULL,
    momentum_score        DOUBLE PRECISION NOT NULL,
    quality_score         DOUBLE PRECISION NOT NULL,
    value_score           DOUBLE PRECISION NOT NULL,
    technical_score       DOUBLE PRECISION NOT NULL,
    news_score            DOUBLE PRECISION NOT NULL,
    supply_demand_score   DOUBLE PRECISION NOT NULL,
    llm_score             DOUBLE PRECISION,
    hybrid_score          DOUBLE PRECISION,
    llm_grade             TEXT,
    llm_reason            TEXT,
    risk_tag              TEXT,
    trade_tier            TEXT,
    is_tradable           BOOLEAN NOT NULL DEFAULT TRUE,
    is_final_selected     BOOLEAN NOT NULL DEFAULT FALSE,
    run_id                TEXT NOT NULL DEFAULT '',
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    imported_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_legacy_quant_date_code_run
    ON legacy_quant_scores (score_date, stock_code, run_id);
CREATE INDEX IF NOT EXISTS ix_legacy_quant_final
    ON legacy_quant_scores (is_final_selected, score_date);
CREATE INDEX IF NOT EXISTS ix_legacy_quant_active
    ON legacy_quant_scores (score_date, is_active);


-- =====================================================================
-- 권한 — 001 전역 정책과 동일
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON legacy_signal_logs, legacy_trade_logs, legacy_quant_scores
    TO pj_runtime;
GRANT SELECT
    ON legacy_signal_logs, legacy_trade_logs, legacy_quant_scores
    TO pj_meta, pj_ui;
