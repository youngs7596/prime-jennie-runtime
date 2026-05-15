-- 2026-05-15 G1 outcome 피드백 view (design: 2026-05-15-scout-overextension-guards.md).
-- Scout 가 직전 추천의 실현 결과를 ScoutContext 로 받기 위한 read-only view.
-- 의사결정 변경 없음 — 데이터 흐름만 마련. 차단은 G2~G4 후행 layer.

BEGIN;

CREATE OR REPLACE VIEW scout_outcomes_v1 AS
WITH buy_agg AS (
    SELECT
        sheet_id,
        SUM(price * qty)::numeric / NULLIF(SUM(qty), 0) AS avg_buy_price,
        MIN(executed_at) AS first_buy_at,
        SUM(qty) AS total_buy_qty
    FROM executions
    WHERE side = 'buy'
    GROUP BY sheet_id
),
sell_agg AS (
    SELECT
        sheet_id,
        SUM(price * qty)::numeric / NULLIF(SUM(qty), 0) AS avg_sell_price,
        MAX(executed_at) AS last_sell_at,
        SUM(qty) AS total_sell_qty,
        -- 분할 체결 시 첫 sell 의 reason 을 대표 reason 으로 (일반적으로 동일).
        -- persistence.py:143 가 'exit_reason' 키로 저장.
        (ARRAY_AGG(metadata_json->>'exit_reason' ORDER BY executed_at))[1] AS exit_reason
    FROM executions
    WHERE side = 'sell'
    GROUP BY sheet_id
)
SELECT
    (ps.provenance_json->>'scout_run_id') AS scout_run_id,
    ps.sheet_id,
    ps.ticker,
    ps.strategy_tag,
    (ps.sheet_json->'sizing'->>'final_pct')::numeric AS final_pct,
    ps.generated_at,
    b.first_buy_at AS bought_at,
    ROUND(b.avg_buy_price, 2) AS entry_price,
    s.last_sell_at AS exit_at,
    ROUND(s.avg_sell_price, 2) AS exit_price,
    s.exit_reason,
    CASE
        WHEN b.avg_buy_price IS NOT NULL AND s.avg_sell_price IS NOT NULL
        THEN ROUND(((s.avg_sell_price / b.avg_buy_price) - 1)::numeric, 4)
        ELSE NULL
    END AS pnl_pct
FROM position_sheets ps
LEFT JOIN buy_agg b ON b.sheet_id = ps.sheet_id
LEFT JOIN sell_agg s ON s.sheet_id = ps.sheet_id
WHERE ps.generated_at > now() - interval '30 days';

GRANT SELECT ON scout_outcomes_v1 TO pj_runtime;

COMMIT;
