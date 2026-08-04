-- 030: KRX 야간선물 미결제약정 상시 수집 (2026-08-04)
--
-- 배경. 028/029 는 하루 4슬롯을 REST(FHMIF10000000)로 찍었는데, 2026-07-26 에 9거래일치로
-- 판정한 결과 **야간 세션이 REST 에 아예 반영되지 않았다**. night_open(18:10) 과
-- night_close(익일 05:05) 가 9/9 일 모든 필드에서 한 자리도 다르지 않았고(OI·거래량·가격·
-- 베이시스 전부), 설계 간판 지표였던 `close.OI − night_close.OI`("낮에 쌓인 포지션 중 밤에
-- 접힌 양")는 구조적으로 항상 0 이었다. 원인은 REST 에 야간장 통로 자체가 없다는 것 —
-- 시장구분 후보를 전수로 넣어 봤지만 `F`(주간) 외엔 INVALID 이거나 빈 output1 이었다.
--
-- 2026-08-04 화요일 야간장에 웹소켓 `H0MFCNT0` 스모크를 10분 돌려 대체 경로를 검증했다.
--   · 근월 A01609 체결 프레임 325개 수신. 같은 10분 동안 REST 는 OI·거래량·가격이 전부
--     불변(164,214 / 137,879 / 1000.0) — "REST 야간 미반영"이 대조 실험으로 증명됐다.
--   · 프레임(49필드) 실측 인덱스: [18]=야간 미결제약정, [19]=주간마감 대비 미결제약정 증감,
--     [0]=종목코드, [1]=체결시각, [5]=현재가, [10]=야간 세션 누적거래량.
--   · **[19] 가 곧 설계 간판 지표다.** 우리가 빼서 만들려던 숫자를 KIS 가 계산해서 준다.
--     산술 2회 독립 검증: 164,214 − 163,821 = 393 = −[19], 164,214 − 163,845 = 369 = −[19].
--
-- 이 표는 야간장(18:00~05:00) 동안 계약별로 분 단위 스냅샷을 남긴다. 세션 마지막 값만
-- 남기지 않는 이유는 야간 중 포지션이 언제 접히는지(초저녁 vs 새벽)까지 보려는 것이고,
-- 분 단위라 한 밤에 계약당 660행을 넘지 않는다.
--
-- trade_date 는 **야간장이 시작된 거래일**이다(18:00 쪽 날짜). 자정을 넘긴 프레임은
-- 하루를 당겨 적재하므로, 같은 trade_date 안에서 주간 close 와 야간 관측이 나란히 놓인다.
-- 028 의 night_close 슬롯과 같은 규약이다.
--
-- 휴장일엔 행을 아예 안 남긴다. 세션 시작 때 그 trade_date 에 futures_oi_snapshots 의
-- close 행이 있는지로 거래일을 판정한다 — 05:00 시점의 '오늘' 기준 판정은 금요일 밤
-- 세션이 토요일 새벽에 끝나는 구조와 어긋나 못 쓰기 때문이고, 이건 028 이 이미 쓰는 가드다.
CREATE TABLE IF NOT EXISTS futures_night_oi (
    trade_date     DATE        NOT NULL,   -- 야간장이 시작된 거래일 (18:00 쪽 날짜)
    contract_code  VARCHAR(10) NOT NULL,   -- A01609 = 정규 KOSPI200 선물 2026-09월물
    minute_ts      TIMESTAMPTZ NOT NULL,   -- 분 단위로 절삭한 관측 시각
    is_front       BOOLEAN     NOT NULL DEFAULT FALSE,
    futures_price  DOUBLE PRECISION,
    open_interest  BIGINT      NOT NULL,   -- 야간 미결제약정 (프레임 [18])
    oi_change      BIGINT,                 -- 주간 마감 대비 증감 (프레임 [19]) = 설계 간판 지표
    night_volume   BIGINT,                 -- 야간 세션 누적거래량 (프레임 [10], 주간과 별도 카운터)
    frames         INTEGER     NOT NULL DEFAULT 0,  -- 그 1분 동안 수신한 체결 프레임 수
    source         TEXT        NOT NULL DEFAULT 'kis_ws',
    PRIMARY KEY (trade_date, contract_code, minute_ts)
);

CREATE INDEX IF NOT EXISTS ix_fno_trade_date ON futures_night_oi (trade_date);

COMMENT ON TABLE futures_night_oi IS
    'KRX 야간선물(H0MFCNT0) 분 단위 미결제약정. REST 는 야간을 못 보므로 웹소켓 전용 경로.';
COMMENT ON COLUMN futures_night_oi.oi_change IS
    '주간 마감 대비 미결제약정 증감. KIS 가 계산해서 주는 값이며, 음수면 밤 사이 포지션이 접힌 것.';
COMMENT ON COLUMN futures_night_oi.night_volume IS
    '야간 세션 누적거래량. 주간 누적거래량과 별개 카운터라 세션 유무 판별에도 쓴다.';
COMMENT ON COLUMN futures_night_oi.frames IS
    '그 분에 수신한 프레임 수. 0 이 아닌 행만 남으므로 결측은 행 부재로만 나타난다.';

GRANT SELECT, INSERT, UPDATE, DELETE ON futures_night_oi TO pj_runtime;
GRANT SELECT ON futures_night_oi TO pj_meta, pj_ui;
