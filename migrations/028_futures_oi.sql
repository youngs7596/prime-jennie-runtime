-- 028: KOSPI200 선물 미결제약정·베이시스 스냅샷 (2026-07-12)
--
-- 배경. "외인 선물 순매수로 코스피 방향을 읽는다"는 글(딴지 889323830)을 시스템화하려다
-- 원천을 실측한 결과:
--   · 투자자별(외인/기관/개인) 선물 순매수 → 수집 불가. KIS 투자자매매동향 API 에 선물
--     시장구분을 넣으면 rt_cd=0 에 전 항목 0(현물 전용), KRX 는 2025-12 회원제 전환으로
--     막혔고 KRX OpenAPI 카탈로그엔 투자자별 거래실적이 없다.
--   · 미결제약정(OI)·OI 증감·베이시스 → 수집 가능. KIS 선물 시세(FHMIF10000000).
--     2026-07-12 실측 A01609("F 202609") OI 157,869 = 글에 실린 HTS 화면과 일치.
-- 글의 주장 자체가 "투자자별 집계는 합성포지션·야간 누락으로 못 믿는다"였으므로, 못 받는
-- 쪽이 어차피 믿을 수 없는 쪽이고 받는 쪽이 저자가 실제 근거로 쓴 관측치다.
--
-- 과거 OI 는 어떤 API 도 주지 않는다(일별 차트 응답은 OHLCV 뿐) → 백필 불가, 전방 적재만.
-- 하루 4개 슬롯을 찍어 "낮에 쌓인 포지션이 밤에 청산되는가"(글의 핵심 주장)를 측정한다:
--   preopen(08:50) / close(15:50) / night_open(18:10) / night_close(익일 05:05)
-- night_close 는 전일 야간장(18:00~05:00) 종료 직후라 trade_date = 캡처일 − 1 로 적재한다.
-- 그러면 같은 trade_date 안에서 (night_close.open_interest − close.open_interest) 가 곧
-- '야간 청산분'이 된다. 야간 세션이 REST 스냅샷에 반영되는지는 아직 미검증 — 네 슬롯을
-- 모두 찍어 추측이 아니라 데이터로 판정한다.
CREATE TABLE IF NOT EXISTS futures_oi_snapshots (
    trade_date        DATE        NOT NULL,
    slot              VARCHAR(12) NOT NULL,   -- preopen | close | night_open | night_close
    contract_code     VARCHAR(10) NOT NULL,   -- A01609 = 정규 KOSPI200 선물 2026-09월물
    contract_name     TEXT,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    futures_price     DOUBLE PRECISION,
    open_interest     BIGINT,                 -- hts_otst_stpl_qty
    oi_change         BIGINT,                 -- otst_stpl_qty_icdc (KIS 가 주는 전일 대비 증감)
    volume            BIGINT,
    basis             DOUBLE PRECISION,       -- 선물 − 현물
    market_basis      DOUBLE PRECISION,
    theoretical_price DOUBLE PRECISION,
    disparity         DOUBLE PRECISION,       -- 괴리율 (선물 − 이론가)
    kospi_index       DOUBLE PRECISION,       -- 코스피 종합지수 (동시각)
    remaining_days    INTEGER,                -- 잔존일수 — 롤오버 추적용
    source            TEXT        NOT NULL DEFAULT 'kis',
    PRIMARY KEY (trade_date, slot, contract_code)
);

CREATE INDEX IF NOT EXISTS ix_fos_trade_date ON futures_oi_snapshots (trade_date);

GRANT SELECT, INSERT, UPDATE, DELETE ON futures_oi_snapshots TO pj_runtime;
GRANT SELECT ON futures_oi_snapshots TO pj_meta, pj_ui;
