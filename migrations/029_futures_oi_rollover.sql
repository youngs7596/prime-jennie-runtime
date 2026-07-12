-- 029: futures_oi_snapshots 롤오버 대응 (2026-07-12, 민지 리뷰 반영)
--
-- 문제: 028 은 근월물 한 계약만 찍었다. 만기 주간엔 미결제약정이 차월물로 이전되면서
-- 근월물 OI 가 급감하는데, 이걸 그대로 델타로 쓰면 '롤오버'가 '청산'으로 오독된다.
-- 판정 표본 60거래일 안에 만기가 최소 한 번 끼므로 판정 시작 전에 반드시 처리해야 한다.
--
-- 대응: 근월·차월 두 계약을 모두 행으로 남긴다(PK 가 이미 contract_code 를 포함해 구조
-- 변경은 불필요). is_front 로 근월물을 표시하고, last_trade_date 로 만기 근접도를 안다.
--   · 분석은 같은 (trade_date, slot) 의 **두 계약 합산 OI** 를 쓰면 롤오버가 중화된다.
--   · 계약별 행이 남아 있으니 이전량(근월 −N ↔ 차월 +N)도 따로 볼 수 있다.
--   · 롤오버 구간은 last_trade_date 로 식별 → 원하면 해당 구간 델타를 제외한다.
ALTER TABLE futures_oi_snapshots
    ADD COLUMN IF NOT EXISTS is_front        BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS last_trade_date DATE;

COMMENT ON COLUMN futures_oi_snapshots.is_front IS
    '근월물(미결제약정 최대) 여부. 롤오버 시 자동으로 차월물로 넘어간다.';
COMMENT ON COLUMN futures_oi_snapshots.last_trade_date IS
    '선물 최종거래일(futs_last_tr_date). 롤오버 구간 식별용.';
