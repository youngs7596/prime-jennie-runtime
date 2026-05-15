-- 2026-05-15 audit D1 fix: runtime state ↔ KIS balance reconcile job.
-- 5분 주기 read-only 검사 + 차이 시 v3:notifications stream 으로 alert 발행.
-- 자동 정리는 안 함 (feedback_sync_positions_manual 메모리 준수).

BEGIN;

INSERT INTO scheduled_jobs (id, owner, handler_key, cron, kwargs, enabled)
VALUES (
    'job_worker.reconcile_state_kis',
    'job_worker',
    'reconcile_state_kis',
    '*/5 9-15 * * 1-5',   -- 거래일 09:00-15:55 매 5분
    '{}'::jsonb,
    true
)
ON CONFLICT (id) DO UPDATE SET
    cron = EXCLUDED.cron,
    handler_key = EXCLUDED.handler_key,
    kwargs = EXCLUDED.kwargs,
    enabled = EXCLUDED.enabled,
    updated_at = now();

COMMIT;
