-- 022_paper_outcomes.sql
-- STOP 상태에서 발행된 시트의 paper PnL 측정 결과 영속.
--
-- v3 의 control STOP 은 fast_loop 스트림 emit 만 차단하고 slow_loop 의 시트 발행은
-- DB 에 그대로 남는다 (publisher.publish(emit_stream=False)). 실거래는 없지만
-- 사후에 "그 시트로 진입했더라면" outcome 을 측정해 시스템 가치 판단의 데이터로 쌓는다.
--
-- 실거래 결과인 outcomes 와 PK·컬럼 구조를 비슷하게 두되,
--  - simulator_version / entry_model / coverage 로 시뮬 환경 식별
--  - scale_out_legs 로 부분 청산 이력 기록
--  - rules_evaluated 로 평가에 사용된 룰 목록
-- 을 추가. simulator 가 업그레이드되면 같은 sheet_id 의 row 를 재계산해
-- UPSERT (computed_at 갱신).

CREATE TABLE IF NOT EXISTS paper_outcomes (
    sheet_id          TEXT PRIMARY KEY REFERENCES position_sheets(sheet_id) ON DELETE CASCADE,
    simulator_version TEXT NOT NULL,                  -- v0 | v1 ...
    entry_model       TEXT NOT NULL,                  -- close_on_publish_date
    coverage          TEXT NOT NULL,                  -- full | no_minute | daily_only
    entry_date        DATE NOT NULL,
    entry_price       NUMERIC NOT NULL,
    exit_date         DATE,
    exit_price        NUMERIC,
    exit_reason       TEXT,                           -- fixed_sl|fixed_tp|trailing_tp|breakeven|
                                                      -- scale_out|time_stop|overextension_exit|
                                                      -- profit_floor|death_cross|window_expired|
                                                      -- data_missing
    holding_days      INT,
    pnl_pct           NUMERIC,                        -- (exit - entry) / entry * 100
    scale_out_legs    JSONB DEFAULT '[]'::JSONB,      -- [{day, price, portion, rule}, ...]
    rules_evaluated   JSONB DEFAULT '[]'::JSONB,      -- ['fixed_sl', 'time_stop', ...]
    metadata_json     JSONB DEFAULT '{}'::JSONB,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_po_entry_date ON paper_outcomes (entry_date);
CREATE INDEX IF NOT EXISTS ix_po_exit_reason ON paper_outcomes (exit_reason);
CREATE INDEX IF NOT EXISTS ix_po_computed_at ON paper_outcomes (computed_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON paper_outcomes TO pj_runtime;
GRANT SELECT ON paper_outcomes TO pj_meta, pj_ui;
