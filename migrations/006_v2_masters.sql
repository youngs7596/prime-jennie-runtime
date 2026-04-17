-- 006_v2_masters.sql
-- v2 포팅: stock_masters + configs.
-- 원본: prime-jennie/migrations/versions/001_initial_schema.py
-- Phase 2.10 Track A — v2 완전 대체를 위해 master 데이터 라이브 테이블로 편입.

-- =====================================================================
-- stock_masters — 종목 마스터 (KOSPI/KOSDAQ)
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_masters (
    stock_code      VARCHAR(10) PRIMARY KEY,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10) NOT NULL DEFAULT 'KOSPI',
    market_cap      BIGINT,
    sector_naver    VARCHAR(50),
    sector_group    VARCHAR(30),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_stock_masters_sector
    ON stock_masters (sector_group);
CREATE INDEX IF NOT EXISTS ix_stock_masters_active
    ON stock_masters (is_active, market);


-- =====================================================================
-- configs — 키/값 런타임 설정 (v2 config_value 는 최대 10000자)
-- =====================================================================

CREATE TABLE IF NOT EXISTS configs (
    config_key      VARCHAR(100) PRIMARY KEY,
    config_value    VARCHAR(10000) NOT NULL,
    description     VARCHAR(500),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- 권한 (001 전역 정책과 동일)
-- =====================================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON stock_masters, configs TO pj_runtime;
GRANT SELECT ON stock_masters, configs TO pj_meta, pj_ui;
