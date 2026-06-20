-- 026_user_directed_dca.sql
-- 사용자 지정 종목 buy-only 시차 분할매수 (user_directed_dca).
--
-- 영석님이 보유 KODEX 200 익절로 확보한 현금을 반도체 대형주(삼성전자·SK하이닉스)에
-- 50:50 으로 넣되, 전액 일시 몰빵 대신 매 영업일 14:30 시장가 분할로 진입한다. 방향은
-- 사용자 뜻대로, 진입 타이밍·사이징만 결정론 규칙으로. slow/fast loop·tracker·시트와
-- 무관한 독립 경로이며 paper 알파 측정에서 구조적으로 제외된다(이 테이블만 쓴다).
--
-- 종목별 독립 트랙 = dca_campaigns 한 row. 슬라이스 시도 1건 = dca_slice_executions 한
-- row 이며 slice_key 가 멱등 고유키(재시작·재전송·같은날 가속+14:30 이중집행 방지).
-- cumulative_filled_krw 가 누적 cap 불변식의 단조증가 진실 공급원 — 시세/잔고에서
-- 역산하지 않는다. 설계: 명세 + 민지 rationale (2026-06-20).

CREATE TABLE IF NOT EXISTS dca_campaigns (
    id              TEXT PRIMARY KEY,               -- "dca:005930:20260620"
    ticker          VARCHAR(6)  NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'active',  -- active|completed|cap_reached|halted
    preset          TEXT        NOT NULL,           -- production|smoke|dryrun (감사용)
    -- 불변 파라미터 (무장 시점 스냅샷)
    cap_krw             BIGINT  NOT NULL,           -- 종목당 누적 상한 (예: 118_000_000)
    total_slices        INTEGER NOT NULL DEFAULT 10,
    execute_at_kst      TEXT    NOT NULL DEFAULT '14:00',  -- 예정 집행 시각 (백테스트 2026-06-20)
    accel_open_pct      NUMERIC NOT NULL DEFAULT -4.0,     -- 당일 시가대비 가속 트리거
    accel_prev_pct      NUMERIC NOT NULL DEFAULT -5.0,     -- 전일 종가대비 가속 트리거
    accel_cooldown_h    INTEGER NOT NULL DEFAULT 24,
    clamp_pct           NUMERIC NOT NULL DEFAULT 0.995,    -- 주문가능금액 클램프 비율
    per_order_cost_cap  BIGINT  NOT NULL,           -- 건별 cost cap (안전망)
    per_day_cost_cap    BIGINT  NOT NULL,           -- 일별 cost cap (안전망)
    dry_run             BOOLEAN NOT NULL DEFAULT TRUE,
    -- 누적 상태 (단조 증가)
    cumulative_filled_krw   BIGINT  NOT NULL DEFAULT 0,
    cumulative_filled_qty   BIGINT  NOT NULL DEFAULT 0,
    slices_done             INTEGER NOT NULL DEFAULT 0,   -- terminal 처리된 예정 슬라이스 수
    -- 가속 쿨다운 / 종료 처리
    last_accel_at   TIMESTAMPTZ,
    final_tail_done BOOLEAN     NOT NULL DEFAULT FALSE,   -- 마지막 잔액 익영업일 1회 추가집행 완료
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_dca_campaigns_status ON dca_campaigns (status);

CREATE TABLE IF NOT EXISTS dca_slice_executions (
    id              BIGSERIAL PRIMARY KEY,
    slice_key       TEXT        NOT NULL,           -- "{campaign_id}:{trade_date}:{slot}" (slot=sched|accel|tail)
    campaign_id     TEXT        NOT NULL REFERENCES dca_campaigns(id) ON DELETE CASCADE,
    ticker          VARCHAR(6)  NOT NULL,
    slice_no        INTEGER     NOT NULL,           -- 1..total_slices, tail=total_slices+1
    trigger_kind    TEXT        NOT NULL,           -- scheduled|acceleration|tail
    trade_date      DATE        NOT NULL,           -- KST 영업일
    status          TEXT        NOT NULL DEFAULT 'pending',
    -- pending|submitted|filled|partial|rejected_retry|passed|skipped_cap
    budget_krw      BIGINT      NOT NULL,           -- 이 시도에 배정된 예산 (roll-over 반영)
    intended_qty    INTEGER,
    order_no        TEXT,
    filled_qty      INTEGER     NOT NULL DEFAULT 0,
    filled_krw      BIGINT      NOT NULL DEFAULT 0,
    avg_price       NUMERIC     NOT NULL DEFAULT 0,
    -- VI 페일세이프 재시도
    retry_at        TIMESTAMPTZ,                    -- non-null = 이 시각 이후 1회 재시도
    retry_count     INTEGER     NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_dca_slice_key UNIQUE (slice_key)
);
CREATE INDEX IF NOT EXISTS ix_dca_slice_campaign_date
    ON dca_slice_executions (campaign_id, trade_date);
CREATE INDEX IF NOT EXISTS ix_dca_slice_retry
    ON dca_slice_executions (retry_at)
    WHERE status = 'rejected_retry';

-- job-worker(tick)·telegram-bot(무장/조회) 는 pj_runtime 으로 접속.
GRANT SELECT, INSERT, UPDATE, DELETE ON dca_campaigns TO pj_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON dca_slice_executions TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE dca_slice_executions_id_seq TO pj_runtime;
GRANT SELECT ON dca_campaigns TO pj_meta, pj_ui;
GRANT SELECT ON dca_slice_executions TO pj_meta, pj_ui;
