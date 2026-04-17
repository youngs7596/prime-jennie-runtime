-- 008_v2_macro.sql
-- v2 포팅: Macro Council 일별 결과 + global snapshot.
-- 원본:
--   prime-jennie/migrations/versions/001_initial_schema.py (daily_macro_insights, global_macro_snapshots)
--   prime-jennie/migrations/versions/002_add_macro_dashboard_fields.py (daily_macro_insights 10 추가 컬럼)
-- 002 merge 완료 스냅샷.

-- =====================================================================
-- daily_macro_insights — Macro Council 일일 판단
-- =====================================================================

CREATE TABLE IF NOT EXISTS daily_macro_insights (
    insight_date                DATE PRIMARY KEY,
    sentiment                   VARCHAR(30) NOT NULL,
    sentiment_score             INTEGER NOT NULL,
    regime_hint                 VARCHAR(200) NOT NULL,
    sectors_to_favor            VARCHAR(500),
    sectors_to_avoid            VARCHAR(500),
    position_size_pct           INTEGER NOT NULL DEFAULT 100,
    stop_loss_adjust_pct        INTEGER NOT NULL DEFAULT 100,
    political_risk_level        VARCHAR(10) NOT NULL DEFAULT 'low',
    political_risk_summary      VARCHAR(2000),
    vix_value                   DOUBLE PRECISION,
    vix_regime                  VARCHAR(20),
    usd_krw                     DOUBLE PRECISION,
    kospi_index                 DOUBLE PRECISION,
    kosdaq_index                DOUBLE PRECISION,
    sector_signals_json         TEXT,
    key_themes_json             TEXT,
    risk_factors_json           TEXT,
    raw_council_output_json     TEXT,
    council_cost_usd            DOUBLE PRECISION,
    data_completeness_pct       INTEGER,
    -- 002_add_macro_dashboard_fields.py 에서 추가된 10 컬럼
    trading_reasoning           TEXT,
    council_consensus           VARCHAR(30),
    strategies_to_favor_json    TEXT,
    strategies_to_avoid_json    TEXT,
    opportunity_factors_json    TEXT,
    kospi_change_pct            DOUBLE PRECISION,
    kosdaq_change_pct           DOUBLE PRECISION,
    kospi_foreign_net           DOUBLE PRECISION,
    kospi_institutional_net     DOUBLE PRECISION,
    kospi_retail_net            DOUBLE PRECISION,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- global_macro_snapshots — 거시 지표 일일 스냅샷
-- =====================================================================

CREATE TABLE IF NOT EXISTS global_macro_snapshots (
    snapshot_date           DATE PRIMARY KEY,
    fed_rate                DOUBLE PRECISION,
    treasury_2y             DOUBLE PRECISION,
    treasury_10y            DOUBLE PRECISION,
    treasury_spread         DOUBLE PRECISION,
    us_cpi_yoy              DOUBLE PRECISION,
    us_unemployment         DOUBLE PRECISION,
    vix                     DOUBLE PRECISION,
    vix_regime              VARCHAR(20),
    dxy_index               DOUBLE PRECISION,
    usd_krw                 DOUBLE PRECISION,
    bok_rate                DOUBLE PRECISION,
    kospi_index             DOUBLE PRECISION,
    kospi_change_pct        DOUBLE PRECISION,
    kosdaq_index            DOUBLE PRECISION,
    kosdaq_change_pct       DOUBLE PRECISION,
    kospi_foreign_net       DOUBLE PRECISION,
    kosdaq_foreign_net      DOUBLE PRECISION,
    kospi_institutional_net DOUBLE PRECISION,
    kospi_retail_net        DOUBLE PRECISION,
    completeness_pct        DOUBLE PRECISION,
    data_sources_json       TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- 권한 (001 전역 정책과 동일)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON daily_macro_insights, global_macro_snapshots TO pj_runtime;
GRANT SELECT
    ON daily_macro_insights, global_macro_snapshots TO pj_meta, pj_ui;
