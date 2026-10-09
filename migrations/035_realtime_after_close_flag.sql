-- 실시간 체결·호가에 '장 마감 뒤' 표시 (2026-10-09).
--
-- 2026-09-14 부터 주간 웹소켓이 장 마감 뒤에도 20:00 까지 체결·호가를 받아 적재했다
-- (넥스트레이드 애프터마켓으로 추정, 시장 구분 필드가 없어 확정은 못 한다). 9-14 이전에도
-- 15:40~16:00 시간외 종가 행이 하루 수백 건씩 있었다. 지우지 않고 표시만 한다.
--
-- 규칙: KST 15:31:00 이후 시각이면 after_close = TRUE, 그 밖은 NULL.
-- 기본값 없는 빈 칸이라 ADD COLUMN 은 테이블을 다시 쓰지 않는다 (호가 20GB).
-- 6980ea3 부터 웹소켓은 15:35 에 닫히고, 적재 코드가 새 행에 같은 규칙으로 표시를 단다.
ALTER TABLE realtime_ticks ADD COLUMN IF NOT EXISTS after_close BOOLEAN;
ALTER TABLE realtime_orderbook ADD COLUMN IF NOT EXISTS after_close BOOLEAN;

COMMENT ON COLUMN realtime_ticks.after_close IS
    'KST 15:31 이후 체결이면 TRUE (장 마감 뒤, KRX 시간외·넥스트레이드 섞임), 정규장은 NULL';
COMMENT ON COLUMN realtime_orderbook.after_close IS
    'KST 15:31 이후 호가면 TRUE (장 마감 뒤, KRX 시간외·넥스트레이드 섞임), 정규장은 NULL';

UPDATE realtime_ticks SET after_close = TRUE
WHERE (ts AT TIME ZONE 'Asia/Seoul')::time >= '15:31' AND after_close IS NULL;

UPDATE realtime_orderbook SET after_close = TRUE
WHERE (ts AT TIME ZONE 'Asia/Seoul')::time >= '15:31' AND after_close IS NULL;
