-- 001_v3_schema.sql
-- Prime Jennie v3 핵심 스키마
-- 6 테이블 + 4 DB 역할

-- =====================================================================
-- 역할 생성
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pj_runtime') THEN
        CREATE ROLE pj_runtime LOGIN PASSWORD 'CHANGE_ME';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pj_meta') THEN
        CREATE ROLE pj_meta LOGIN PASSWORD 'CHANGE_ME';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pj_ui') THEN
        CREATE ROLE pj_ui LOGIN PASSWORD 'CHANGE_ME';
    END IF;
END
$$;

-- =====================================================================
-- 1. position_sheets — 느린↔빠른 루프 유일한 인터페이스
-- =====================================================================

CREATE TABLE IF NOT EXISTS position_sheets (
    sheet_id        TEXT PRIMARY KEY,                -- ps_YYYYMMDD_TICKER_XXXX
    schema_version  TEXT NOT NULL DEFAULT '1.1',
    generated_at    TIMESTAMPTZ NOT NULL,
    valid_until     TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    strategy_tag    TEXT NOT NULL,
    sheet_json      JSONB NOT NULL,                  -- 전체 시트 JSON
    provenance_json JSONB NOT NULL,                  -- provenance 섹션
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ps_generated_at ON position_sheets (generated_at);
CREATE INDEX IF NOT EXISTS ix_ps_ticker ON position_sheets (ticker);
CREATE INDEX IF NOT EXISTS ix_ps_strategy_tag ON position_sheets (strategy_tag);
CREATE INDEX IF NOT EXISTS ix_ps_scout_run ON position_sheets ((provenance_json->>'scout_run_id'));

-- =====================================================================
-- 2. executions — 체결 기록
-- =====================================================================

CREATE TABLE IF NOT EXISTS executions (
    execution_id    BIGSERIAL PRIMARY KEY,
    sheet_id        TEXT NOT NULL REFERENCES position_sheets(sheet_id),
    side            TEXT NOT NULL,                    -- buy | sell
    price           NUMERIC NOT NULL,
    qty             INT NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL,
    slippage_bps    NUMERIC,
    metadata_json   JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_exec_sheet ON executions (sheet_id);
CREATE INDEX IF NOT EXISTS ix_exec_at ON executions (executed_at);

-- =====================================================================
-- 3. outcomes — 매매 결과 (시트당 1건)
-- =====================================================================

CREATE TABLE IF NOT EXISTS outcomes (
    sheet_id          TEXT PRIMARY KEY REFERENCES position_sheets(sheet_id),
    entry_price       NUMERIC NOT NULL,
    exit_price        NUMERIC,
    holding_period_s  INT,
    pnl_krw           NUMERIC,
    pnl_pct           NUMERIC,
    exit_reason       TEXT,                           -- tp|sl|breakeven|scale_out|time|profit_floor|death_cross|overextension|manual|unfilled
    closed_at         TIMESTAMPTZ,
    metadata_json     JSONB DEFAULT '{}'
);

-- =====================================================================
-- 4. scout_runs — Scout 실행 이력
-- =====================================================================

CREATE TABLE IF NOT EXISTS scout_runs (
    scout_run_id    TEXT PRIMARY KEY,                 -- scout_YYYYMMDD_HHMM
    generated_at    TIMESTAMPTZ NOT NULL,
    code_hash       TEXT NOT NULL,                    -- sha256
    code_text       TEXT NOT NULL,                    -- 실행된 Python 코드
    hypothesis      TEXT,
    candidates_count INT,
    model_used      TEXT,
    prompt_version  TEXT,
    cost_usd        NUMERIC,
    metadata_json   JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_scout_at ON scout_runs (generated_at);

-- =====================================================================
-- 5. macro_runs — Macro Gate 실행 이력
-- =====================================================================

CREATE TABLE IF NOT EXISTS macro_runs (
    macro_run_id       TEXT PRIMARY KEY,              -- macro_YYYYMMDD_HHMM
    generated_at       TIMESTAMPTZ NOT NULL,
    trigger_reason     TEXT NOT NULL,                 -- scheduled_0800 | manual | auto_*
    gate               TEXT NOT NULL,                 -- open | closed
    size_multiplier    NUMERIC NOT NULL,
    reasoning          TEXT,
    top_risks_json     JSONB,
    confidence         TEXT,                          -- high | medium | low
    news_digest_ref    TEXT,
    next_review_hint   TEXT,
    prompt_version     TEXT,
    model_used         TEXT,
    cost_usd           NUMERIC,
    latency_ms         INT,
    auto_override      BOOLEAN DEFAULT FALSE,
    metadata_json      JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_macro_at ON macro_runs (generated_at);

-- =====================================================================
-- 6. meta_prs — Meta PR 이력
-- =====================================================================

CREATE TABLE IF NOT EXISTS meta_prs (
    pr_number       INT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    risk_category   TEXT,                             -- scout_code | eval_logic | executor_rule
    stage           INT NOT NULL DEFAULT 0,
    eval_run_id     TEXT,
    merged_at       TIMESTAMPTZ,
    reverted_by     INT REFERENCES meta_prs(pr_number),
    metadata_json   JSONB DEFAULT '{}'
);

-- =====================================================================
-- 권한 부여
-- =====================================================================

-- pj_runtime: 전체 읽기/쓰기
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pj_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pj_runtime;

-- pj_meta, pj_ui: 읽기 전용
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pj_meta;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pj_ui;

-- 향후 생성될 테이블에도 동일 권한 적용
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pj_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO pj_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO pj_meta;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO pj_ui;
