-- 009_v2_trading_ops.sql
-- v2 포팅: 운영 Trading 상태 테이블 3종.
-- 원본:
--   prime-jennie/migrations/versions/001_initial_schema.py (positions, daily_asset_snapshots, watchlist_histories)
--   prime-jennie/migrations/versions/005_add_watchlist_history_columns.py (quant_score, sector_group, market_regime)
--   prime-jennie/migrations/versions/008_add_run_id_to_history_tables.py (run_id, is_active + PK 확장)

-- =====================================================================
-- positions — 현재 보유 포지션 (종목당 1행)
-- =====================================================================

CREATE TABLE IF NOT EXISTS positions (
    stock_code              VARCHAR(10) PRIMARY KEY REFERENCES stock_masters(stock_code),
    stock_name              VARCHAR(100) NOT NULL,
    quantity                INTEGER NOT NULL,
    average_buy_price       INTEGER NOT NULL,
    total_buy_amount        INTEGER NOT NULL,
    sector_group            VARCHAR(30),
    high_watermark          INTEGER,
    stop_loss_price         INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- daily_asset_snapshots — 일별 계좌 자산 스냅샷
-- =====================================================================

CREATE TABLE IF NOT EXISTS daily_asset_snapshots (
    snapshot_date           DATE PRIMARY KEY,
    total_asset             BIGINT NOT NULL,
    cash_balance            BIGINT NOT NULL,
    stock_eval_amount       BIGINT NOT NULL,
    total_profit_loss       BIGINT,
    realized_profit_loss    BIGINT,
    net_investment          BIGINT,
    position_count          INTEGER NOT NULL DEFAULT 0
);


-- =====================================================================
-- watchlist_histories — 일별 워치리스트 스냅샷
-- v2 005: quant_score / sector_group / market_regime 추가
-- v2 008: run_id / is_active 추가 + PK (snapshot_date, stock_code, run_id) 확장
-- =====================================================================

CREATE TABLE IF NOT EXISTS watchlist_histories (
    snapshot_date       DATE NOT NULL,
    stock_code          VARCHAR(10) NOT NULL REFERENCES stock_masters(stock_code),
    run_id              VARCHAR(30) NOT NULL DEFAULT '',
    stock_name          VARCHAR(100) NOT NULL,
    llm_score           DOUBLE PRECISION,
    hybrid_score        DOUBLE PRECISION,
    is_tradable         BOOLEAN NOT NULL DEFAULT TRUE,
    trade_tier          VARCHAR(10),
    risk_tag            VARCHAR(30),
    rank                INTEGER,
    quant_score         DOUBLE PRECISION,
    sector_group        VARCHAR(30),
    market_regime       VARCHAR(20),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT pk_watchlist_histories PRIMARY KEY (snapshot_date, stock_code, run_id)
);

CREATE INDEX IF NOT EXISTS ix_watchlist_active
    ON watchlist_histories (snapshot_date, is_active);


-- =====================================================================
-- daily_quant_scores — Scout/Quant 일일 스코어 (v2 운영 테이블 동일명)
-- 원본:
--   001_initial_schema.py (기본 컬럼)
--   006_add_daily_quant_scores_llm_grade.py (llm_grade)
--   008_add_run_id_to_history_tables.py (run_id, is_active)
-- ※ legacy_quant_scores (002_legacy_namespace.sql) 와는 분리된 라이브 테이블.
--   legacy_* 는 MariaDB 컷오버 1회용 아카이브, 이 테이블은 v3 신규 실행 기록.
-- =====================================================================

CREATE TABLE IF NOT EXISTS daily_quant_scores (
    id                      BIGSERIAL PRIMARY KEY,
    score_date              DATE NOT NULL,
    stock_code              VARCHAR(10) NOT NULL REFERENCES stock_masters(stock_code),
    stock_name              VARCHAR(100) NOT NULL,
    total_quant_score       DOUBLE PRECISION NOT NULL,
    momentum_score          DOUBLE PRECISION NOT NULL,
    quality_score           DOUBLE PRECISION NOT NULL,
    value_score             DOUBLE PRECISION NOT NULL,
    technical_score         DOUBLE PRECISION NOT NULL,
    news_score              DOUBLE PRECISION NOT NULL,
    supply_demand_score     DOUBLE PRECISION NOT NULL,
    llm_score               DOUBLE PRECISION,
    hybrid_score            DOUBLE PRECISION,
    llm_grade               VARCHAR(5),
    llm_reason              VARCHAR(2000),
    risk_tag                VARCHAR(30),
    trade_tier              VARCHAR(10),
    is_tradable             BOOLEAN NOT NULL DEFAULT TRUE,
    is_final_selected       BOOLEAN NOT NULL DEFAULT FALSE,
    run_id                  VARCHAR(30) NOT NULL DEFAULT '',
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT uq_quant_date_code_run UNIQUE (score_date, stock_code, run_id)
);

CREATE INDEX IF NOT EXISTS ix_quant_final
    ON daily_quant_scores (is_final_selected, score_date);
CREATE INDEX IF NOT EXISTS ix_quant_active
    ON daily_quant_scores (score_date, is_active);


-- =====================================================================
-- 권한 (001 전역 정책과 동일)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON positions, daily_asset_snapshots, watchlist_histories, daily_quant_scores
    TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE daily_quant_scores_id_seq TO pj_runtime;
GRANT SELECT
    ON positions, daily_asset_snapshots, watchlist_histories, daily_quant_scores
    TO pj_meta, pj_ui;
