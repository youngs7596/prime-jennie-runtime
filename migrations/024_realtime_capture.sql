-- 024_realtime_capture.sql
-- KIS 실시간 체결·호가 적재 (게이트웨이가 직접 기록).
--
-- 실시간 웹소켓으로 들어오는 체결과 호가는 지나가면 KIS 에서 과거로 다시 받을 수
-- 없다 (체결내역 조회 3개월, 분봉 며칠 한계). 게이트웨이가 보유·추천시트 종목
-- (KIS 등록 한도상 체결+호가 두 채널이면 약 20종목) 의 틱과 호가를 받아 여기에 쌓는다.
-- 용도는 진입 타이밍·체결 품질·슬리피지 분석. 분석 형태가 정해지지 않아 원본에 가깝게 둔다.
--
-- 적재는 게이트웨이 안 메모리 버퍼 → 1~2초 묶음 INSERT (틱마다 DB 를 치면 웹소켓
-- 응답이 밀려 KIS 가 연결을 끊는다). 보존은 jobs.maintenance.cleanup_old_data 가 정리.
-- 설계: .ai/designs/2026-06-16-realtime-orderbook-capture.md

CREATE TABLE IF NOT EXISTS realtime_ticks (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  VARCHAR(10) NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,        -- KIS 체결시각 (체결시각 HHMMSS + 당일)
    price       INTEGER     NOT NULL,        -- 체결가
    volume      INTEGER     NOT NULL,        -- 이 틱 체결량 (CNTG_VOL)
    cum_volume  BIGINT,                      -- 누적거래량 (ACML_VOL)
    trade_sign  SMALLINT,                    -- 체결구분 (1:매수 3:장전 5:매도)
    strength    DOUBLE PRECISION,            -- 체결강도 (CTTR)
    best_ask    INTEGER,                     -- 최우선 매도호가
    best_bid    INTEGER,                     -- 최우선 매수호가
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_rt_ticks_code_ts ON realtime_ticks (stock_code, ts);

CREATE TABLE IF NOT EXISTS realtime_orderbook (
    id           BIGSERIAL PRIMARY KEY,
    stock_code   VARCHAR(10) NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,       -- KIS 영업시각 (HHMMSS + 당일)
    ask_prices   INTEGER[]   NOT NULL,       -- 매도호가 1~10
    ask_volumes  INTEGER[]   NOT NULL,       -- 매도호가 잔량 1~10
    bid_prices   INTEGER[]   NOT NULL,       -- 매수호가 1~10
    bid_volumes  INTEGER[]   NOT NULL,       -- 매수호가 잔량 1~10
    total_ask    BIGINT,                     -- 총 매도호가 잔량
    total_bid    BIGINT,                     -- 총 매수호가 잔량
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_rt_ob_code_ts ON realtime_orderbook (stock_code, ts);

-- 게이트웨이는 pj_runtime 역할로 접속해 INSERT, cleanup 잡은 DELETE.
GRANT SELECT, INSERT, UPDATE, DELETE ON realtime_ticks TO pj_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON realtime_orderbook TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE realtime_ticks_id_seq TO pj_runtime;
GRANT USAGE, SELECT ON SEQUENCE realtime_orderbook_id_seq TO pj_runtime;
GRANT SELECT ON realtime_ticks TO pj_meta, pj_ui;
GRANT SELECT ON realtime_orderbook TO pj_meta, pj_ui;
