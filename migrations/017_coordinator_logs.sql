-- 017_coordinator_logs.sql
-- Agent Coordinator (Harness 패턴) 의 decision_log + event_log.
-- design: .ai/designs/2026-05-14-agent-coordinator.md
-- 메모리: project_v3_orchestrator_decision.md
--
-- decision_log: Coordinator 의 모든 결정 (entry/exit/sheet_publish/policy_change) 기록.
--   advisory mode (Stage 1) 에선 observe 만, enforce mode (Stage 3) 에서 실제 차단.
--   context_snapshot_json 이 결정 시점의 시스템 상태 전체 보존 → multi-dim retrospective.
--
-- event_log: 모든 Coordinator Event Bus 이벤트 archive.
--   sheet_published / entry_filled / exit_filled / policy_changed / external_event 등.
--   decision_log 와 별개로 시간축 event stream 영구 보존.

BEGIN;

CREATE TABLE IF NOT EXISTS decision_log (
    decision_id           BIGSERIAL PRIMARY KEY,
    kind                  TEXT NOT NULL,
        -- entry_decision | exit_decision | sheet_publish_decision | policy_change_decision
    subject_id            TEXT,
        -- 결정 대상 식별자. entry/exit/sheet_publish 면 sheet_id, policy_change 면 policy_name
    outcome               TEXT NOT NULL,
        -- ok | not_ok | advisory_ok | advisory_not_ok | error
    reason                TEXT,
        -- 결정 사유 코드 (예: duplicate_today, recent_stoploss_cooldown, macro_closed, conviction_too_low)
    policy_name           TEXT,
        -- 발화된 policy 식별자 (예: ConvictionSizingPolicy, DuplicateTodayPolicy)
    context_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- DecisionContext (보유/큐/거래 history/macro/risk/시스템 health/발행 timing) snapshot
    decided_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_decision_log_decided_at ON decision_log (decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_decision_log_kind_outcome ON decision_log (kind, outcome);
CREATE INDEX IF NOT EXISTS ix_decision_log_subject ON decision_log (subject_id) WHERE subject_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_decision_log_policy ON decision_log (policy_name) WHERE policy_name IS NOT NULL;

COMMENT ON TABLE decision_log IS 'Agent Coordinator decision log — Stage 1 advisory, Stage 3 enforce. context_snapshot_json 이 Phase 0 retrospective 분석의 source';
COMMENT ON COLUMN decision_log.kind IS 'entry_decision | exit_decision | sheet_publish_decision | policy_change_decision';
COMMENT ON COLUMN decision_log.outcome IS 'ok | not_ok | advisory_ok | advisory_not_ok | error';
COMMENT ON COLUMN decision_log.context_snapshot_json IS 'DecisionContext 의 시점 snapshot — multi-dim 분석용';


CREATE TABLE IF NOT EXISTS event_log (
    event_id              BIGSERIAL PRIMARY KEY,
    event_kind            TEXT NOT NULL,
        -- sheet_published | entry_queued | entry_decided | entry_filled | entry_rejected
        -- exit_decided | exit_filled | policy_changed | external_event
    subject_id            TEXT,
        -- 이벤트 대상. sheet/entry/exit 면 sheet_id, policy_change 면 policy_name
    ticker                TEXT,
        -- 대상 종목 (조회 편의)
    payload_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- 이벤트별 payload (Pydantic event schema 직렬화)
    correlation_id        TEXT,
        -- 한 sheet 의 publish → filled → exited 까지 묶는 id. 보통 sheet_id 와 동일하지만
        -- policy/external 은 별도 id
    decision_id           BIGINT,
        -- 이 이벤트가 어떤 결정 결과인지 (있을 때만). FK 강제 X — Coordinator 가
        -- 결정 없이 직접 발행하는 이벤트도 있음 (entry_filled 등 사후 기록)
    stream_msg_id         TEXT,
        -- 발행 stream 의 XADD message id. listener 가 ON CONFLICT DO NOTHING 으로
        -- ACK 실패 후 재배달 시 중복 INSERT 방지 (Stage 1 idempotency key).
    occurred_at           TIMESTAMPTZ NOT NULL,
        -- 실제 이벤트 발생 시각 (agent 가 채움)
    recorded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        -- Coordinator 가 받아서 기록한 시각
);

CREATE INDEX IF NOT EXISTS ix_event_log_occurred_at ON event_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_event_log_kind ON event_log (event_kind);
CREATE INDEX IF NOT EXISTS ix_event_log_subject ON event_log (subject_id) WHERE subject_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_event_log_ticker ON event_log (ticker) WHERE ticker IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_event_log_correlation ON event_log (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_log_stream_msg_id
    ON event_log (stream_msg_id) WHERE stream_msg_id IS NOT NULL;

COMMENT ON TABLE event_log IS 'Agent Coordinator Event Bus archive — sheet/entry/exit/policy/external events. correlation_id 로 시트 lifecycle 추적';
COMMENT ON COLUMN event_log.event_kind IS 'sheet_published | entry_queued | entry_decided | entry_filled | entry_rejected | exit_decided | exit_filled | policy_changed | external_event';
COMMENT ON COLUMN event_log.correlation_id IS '시트 lifecycle 묶는 id. 보통 sheet_id. policy/external 은 별도';

COMMIT;
