-- 012_backtest_persistence.sql
-- 백테스트/재현성 축적용 영속화 레이어 보강.
--
-- 현재 scout_runs 는 code_text 만 남기고 실행 입력 context 는 휘발. 시트로 승격되지
-- 않은 raw 후보 티커도 유실. 과거 Scout 실행을 "같은 코드 × 같은 입력" 으로 재현하려면
-- 아래 세 가지가 필요:
--   (1) scout_runs.context_snapshot_json — news_scores/sector_momentum/macro_size
--       multiplier/universe_hash 의 생성 시점 스냅샷
--   (2) screening_candidates — 코드 실행 직후 raw 후보 전수 (승격/탈락 사유 포함)
--   (3) position_sheets.provenance_json.macro_run_id — Macro Council run 직접 참조
--       (현재 macro_state_snapshot.gate_run_id 만 있음 → 동일 값이지만 탑-레벨에도 박아
--        UI 조인을 단순화)

-- =====================================================================
-- 1) scout_runs.context_snapshot_json
--    metadata_json 은 Scout 출력 부가정보(factor_weights 등) 전용으로 보존하고
--    Scout 입력 context 는 별도 컬럼으로 분리 — 재현 복원 시 SQL 한 번에 SELECT.
-- =====================================================================

ALTER TABLE scout_runs
    ADD COLUMN IF NOT EXISTS context_snapshot_json JSONB DEFAULT '{}';


-- =====================================================================
-- 2) screening_candidates — Scout 코드가 반환한 raw 후보 전수
--    scout_run 당 최대 20건 (CANDIDATE_HARD_CAP). 시트 승격 여부는 sheet_id /
--    rejection_reason 으로 추적. 둘 다 NULL 이면 "아직 Strategy Engine 가 처리
--    중" 의 transient state.
-- =====================================================================

CREATE TABLE IF NOT EXISTS screening_candidates (
    scout_run_id         TEXT NOT NULL REFERENCES scout_runs(scout_run_id) ON DELETE CASCADE,
    rank                 INT NOT NULL,                              -- Scout 코드 반환 순서 (0-indexed)
    ticker               TEXT NOT NULL,
    strategy_tag         TEXT NOT NULL,
    conviction           NUMERIC(4, 3),                              -- 0.000 ~ 1.000
    entry_hint_json      JSONB,
    exit_hint_json       JSONB,
    factors_json         JSONB,                                      -- 후보 선정 근거 팩터
    notes                TEXT,
    promoted_to_sheet_id TEXT REFERENCES position_sheets(sheet_id),  -- NULL: 미승격
    rejection_reason     TEXT,                                       -- macro_closed | deprecated_tag | unknown_tag | no_policy | duplicate_today | size_below_min | validator_failed | null
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scout_run_id, rank)
);

-- 백테스트 질의 패턴:
-- - 특정 scout_run 전수: (scout_run_id) — PK 커버
-- - 종목별 히스토리: (ticker, created_at DESC)
-- - 승격률/탈락률 집계: (promoted_to_sheet_id), (rejection_reason)
CREATE INDEX IF NOT EXISTS ix_sc_ticker_at
    ON screening_candidates (ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_sc_promoted
    ON screening_candidates (promoted_to_sheet_id)
    WHERE promoted_to_sheet_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sc_rejection
    ON screening_candidates (rejection_reason)
    WHERE rejection_reason IS NOT NULL;


-- =====================================================================
-- 3) position_sheets provenance 에 macro_run_id 인덱스
--    ProvenanceSection 에 top-level macro_run_id 를 덧박음 (schema 변경은 Pydantic
--    optional 필드 추가). 기존 row 는 NULL — WHERE 로 partial 인덱싱.
-- =====================================================================

CREATE INDEX IF NOT EXISTS ix_ps_macro_run
    ON position_sheets ((provenance_json->>'macro_run_id'))
    WHERE provenance_json->>'macro_run_id' IS NOT NULL;


-- =====================================================================
-- 4) 권한
--    001 의 ALTER DEFAULT PRIVILEGES 가 자동 부여하지만, 011 패턴 따라 명시.
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON screening_candidates TO pj_runtime;
GRANT SELECT ON screening_candidates TO pj_meta, pj_ui;
