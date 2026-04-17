-- 005_scheduled_jobs.sql
-- v3 자체 스케줄 레지스트리. apscheduler 내장 pickle jobstore 대신
-- 평범한 테이블로 두어 control-ui 가 SQL 로 CRUD 할 수 있게 한다.

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id              TEXT PRIMARY KEY,                -- 사람이 읽는 식별자 e.g. "news_pipeline.crawl_cycle"
    owner           TEXT NOT NULL,                   -- "news_pipeline" | "price_scheduler" | ...
    handler_key     TEXT NOT NULL,                   -- owner 내부 dispatch 키 e.g. "crawl_cycle"
    cron            TEXT NOT NULL,                   -- crontab 표현식 (5-field)
    kwargs          JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT,                            -- "running" | "success" | "failed"
    last_error      TEXT,
    last_duration_ms INTEGER,
    next_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_owner_enabled
    ON scheduled_jobs (owner, enabled);


-- 실행 이력 (control-ui 에서 최근 N회 실행 조회용). 필요시 파티셔닝 고려.
CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL,                   -- "running" | "success" | "failed"
    error           TEXT,
    duration_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS ix_scheduled_job_runs_job_started
    ON scheduled_job_runs (job_id, started_at DESC);


-- pj_runtime: 스케줄러가 자기 소관 job 을 읽고 실행 상태를 갱신
GRANT SELECT, INSERT, UPDATE, DELETE
    ON scheduled_jobs, scheduled_job_runs TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE scheduled_job_runs_run_id_seq TO pj_runtime;

-- pj_ui: 조회 + cron/enabled/kwargs 편집. 실행 상태 필드는 건드리지 않음.
GRANT SELECT ON scheduled_jobs, scheduled_job_runs TO pj_ui;
GRANT UPDATE (cron, enabled, kwargs, handler_key, updated_at) ON scheduled_jobs TO pj_ui;
GRANT INSERT, DELETE ON scheduled_jobs TO pj_ui;

-- pj_meta: 읽기 전용
GRANT SELECT ON scheduled_jobs, scheduled_job_runs TO pj_meta;
