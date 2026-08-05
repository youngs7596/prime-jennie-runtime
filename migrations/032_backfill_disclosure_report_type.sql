-- 032: 공시 유형(report_type) 옛 행 채우기 (2026-08-05)
--
-- 크롤러가 DART list.json 응답 row 에서 `pblntf_ty` 를 읽어 report_type 에 넣었는데,
-- **그 응답에는 공시 유형 필드가 아예 없다**(실측 필드: corp_code · corp_name ·
-- stock_code · corp_cls · report_nm · rcept_no · flr_nm · rcept_dt · rm). 그래서
-- 이 컬럼이 47,752행 전부 NULL 이었다. 크롤러는 이제 조회에 쓴 유형을 그대로 찍는다.
--
-- 옛 행을 되살릴 수 있는지 제목으로 따져 봤다. 2026-03 이후(v3 가 정기공시만 받던 구간)는
-- 100% 정기공시이고, 2026-02 이전(v2 시절)은 임원·주요주주 소유상황보고서, 증권발행실적
-- 보고서, 기업설명회 개최 같은 다른 유형이 섞여 있다 — v2 는 넓게 받았다는 뜻이다.
--
-- 그래서 **제목이 사업·반기·분기보고서인 행만 'A'(정기공시)로 채운다**. 이건 시기와
-- 무관하게 참이다(실측 상위 제목이 전부 "분기보고서 (2026.03)" 꼴). 나머지 42,100행은
-- v2 시절 것이고 제목만으로 유형 코드를 정확히 되돌릴 수 없어 **NULL 로 남긴다** —
-- 틀린 코드를 박아 넣느니 모른다고 두는 쪽이 낫다. 소비할 때 NULL 은 "2026-08-05 이전
-- 적재라 유형 미상"으로 읽으면 된다.

BEGIN;

UPDATE stock_disclosures
SET report_type = 'A'
WHERE report_type IS NULL
  AND title ~ '(사업|반기|분기)보고서';

-- 채우고 난 분포 확인 (psql 출력에 찍힌다).
SELECT coalesce(report_type, '(유형 미상 — v2 시절 적재)') AS report_type,
       count(*) AS rows
FROM stock_disclosures
GROUP BY 1
ORDER BY 1;

COMMIT;
