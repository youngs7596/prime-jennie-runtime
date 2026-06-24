-- 027_market_inputs.sql
-- 매크로 게이트 입력 고도화: VKOSPI 일별 + 시장전체 투자자 수급(연기금 분리).
--
-- 배경: 2026-06-23 한국발 폭락이 펀더멘털이 아니라 수급 충격(국민연금 리밸런싱 매도)임을
-- 당일 판정하려 했으나, 운영 DB 는 기관을 한 덩어리로만 받고(네이버 frgn, 연기금 분리 없음)
-- 변동성지수도 안 받아 못 봤다. 두 입력을 정식으로 적재한다.
--
-- 출처(2026-06-24 검증): VKOSPI 는 KRX 웹포털·OpenAPI 가 둘 다 안 줘서(포털 안티봇 LOGOUT,
-- OpenAPI 카탈로그에 변동성지수 없음) CNBC 내부 차트 API(.KSVKOSPI)가 유일 동작 출처.
-- 연기금 분리 수급은 KRX 가 막혔고 OpenAPI 에 투자자 서비스가 없어, 네이버 시장전체 페이지
-- (investorDealTrendDay, 기관을 금융투자·연기금등 등으로 분해)에서 무인증으로 받는다.
-- 둘 다 비공식 출처라 수집기에서 값 sanity 가드를 둔다.

-- VKOSPI(코스피200 변동성지수) 일별 — CNBC .KSVKOSPI. close 가 주신호, OHLC 보조.
CREATE TABLE IF NOT EXISTS vkospi_daily (
    price_date   DATE PRIMARY KEY,
    open_price   DOUBLE PRECISION,
    high_price   DOUBLE PRECISION,
    low_price    DOUBLE PRECISION,
    close_price  DOUBLE PRECISION NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'cnbc',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 시장전체(KOSPI/KOSDAQ) 일별 투자자유형별 순매수 — 네이버 investorDealTrendDay.
-- 핵심은 연기금(pension_net)이 기관계(institution_net)와 별개로 떨어진다는 것.
-- 값 단위는 네이버 페이지 원시값(백만원), 부호는 순매수(+매수/−매도).
CREATE TABLE IF NOT EXISTS market_investor_flows (
    trade_date        DATE        NOT NULL,
    market            VARCHAR(10) NOT NULL,           -- KOSPI | KOSDAQ
    individual_net    DOUBLE PRECISION,               -- 개인
    foreign_net       DOUBLE PRECISION,               -- 외국인
    institution_net   DOUBLE PRECISION,               -- 기관계
    financial_inv_net DOUBLE PRECISION,               -- 금융투자
    insurance_net     DOUBLE PRECISION,               -- 보험
    trust_net         DOUBLE PRECISION,               -- 투신(사모)
    bank_net          DOUBLE PRECISION,               -- 은행
    etc_finance_net   DOUBLE PRECISION,               -- 기타금융기관
    pension_net       DOUBLE PRECISION,               -- 연기금등
    etc_corp_net      DOUBLE PRECISION,               -- 기타법인
    unit              TEXT        NOT NULL DEFAULT 'million_krw',
    source            TEXT        NOT NULL DEFAULT 'naver',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trade_date, market)
);
CREATE INDEX IF NOT EXISTS ix_mif_trade_date ON market_investor_flows (trade_date);

GRANT SELECT, INSERT, UPDATE, DELETE ON vkospi_daily TO pj_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON market_investor_flows TO pj_runtime;
GRANT SELECT ON vkospi_daily TO pj_meta, pj_ui;
GRANT SELECT ON market_investor_flows TO pj_meta, pj_ui;
