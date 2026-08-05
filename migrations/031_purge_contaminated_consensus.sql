-- 031: 종목이 뒤바뀐 컨센서스 8거래일치 삭제 (2026-08-05)
--
-- FnGuide 가 기업정보를 comp.fnguide.com/SVO2/ASP/SVD_Main.asp?gicode=A종목코드 에서
-- wcomp.fnguide.com/CompanyInfo/Snapshot?cmp_cd=A종목코드 로 옮겼다. 이사 직후 옛 주소가
-- 새 사이트로 넘어가면서 파라미터 이름 gicode 가 무시됐고, 새 사이트는 종목을 못 알아들으면
-- 기본 페이지인 삼성전자를 보여준다. 그래서 2026-07-02 부터 07-27 까지 여덟 거래일 동안
-- 213 종목이 전부 삼성전자 숫자를 받아 적었다.
--
-- 오염은 날짜별 서로 다른 값의 개수로 드러난다:
--   06-29  208행 / 목표주가 189가지  ← 정상
--   07-02  211행 /   2가지
--   07-06 ~ 07-27 매일 1가지          ← 전 종목 동일
-- 같은 날부터 forward_roe 채움률도 208/208 에서 0 으로 떨어졌다(업종비교 표가 그 페이지에서
-- 빠졌다). 07-30 부터는 옛 주소가 아예 안내문을 주면서 FnGuide 행 자체가 사라지고 네이버
-- 폴백만 남았다 — 그쪽은 종목별로 값이 다르므로(216행 중 EPS 209가지) 건드리지 않는다.
--
-- 컨센서스는 "오늘 값"만 주는 스냅샷이라 과거로 되받을 수 없다. 복구가 아니라 삭제다.
-- 남겨 두면 팩터 분석과 EPS 30일 변화율이 이 구간을 진짜 데이터로 읽는다.
--
-- 재발 방지는 코드 쪽에 두 겹으로 들어갔다: 크롤러가 페이지 제목의 종목코드를 조회한
-- 코드와 대조하고(우선주가 보통주로 바꿔치기되는 경우도 같은 검사로 걸린다), 수집 잡이
-- 끝에서 "전 종목 같은 값"이면 예외를 던져 실패로 기록한다.

BEGIN;

-- 지우기 전 상태를 로그로 남긴다 (psql 출력에 찍힌다).
SELECT trade_date,
       count(*)                        AS rows_to_delete,
       count(DISTINCT target_price)    AS distinct_target
FROM stock_consensus
WHERE source = 'FNGUIDE'
  AND trade_date BETWEEN DATE '2026-07-02' AND DATE '2026-07-27'
GROUP BY trade_date
ORDER BY trade_date;

DELETE FROM stock_consensus
WHERE source = 'FNGUIDE'
  AND trade_date BETWEEN DATE '2026-07-02' AND DATE '2026-07-27';

COMMIT;
