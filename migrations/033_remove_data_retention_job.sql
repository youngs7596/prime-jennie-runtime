-- 033: 오래된 데이터를 지우던 잡 제거 (2026-08-23)
--
-- `job_worker.cleanup_old_data` 는 매일 새벽 3시에 `daily_prices` 에서 365일 넘은
-- 행을 지우고 있었다. 근거는 코드 주석에 이렇게 적혀 있었다 — "일봉은 KIS 에서
-- 언제든 다시 받을 수 있어 정리해도 무방하다".
--
-- 그 문장이 사실이 아니다. **KIS 는 어떤 API 로도 3개월 이전 데이터를 주지 않는다.**
-- 우리가 수집 잡을 따로 돌려 매일 쌓고 있는 이유가 바로 그것이다. v2 에서 물려받은
-- 문장인데 v2 시절에도 맞지 않았고, 실제로 다시 받아 본 적도 없다.
--
-- 지금까지 실제로 지워진 행은 없다. 삭제 기준일이 2025-08-23 이고 유니버스 일봉은
-- 2026-03-09 부터라 아직 안 닿았다. 이 잡을 그대로 두면 2027-03 부터 복구 불가능한
-- 손실이 시작된다.
--
-- 저장공간은 판단 근거가 아니다. 일봉은 상위 300종목 기준 연 7만 5천 행이다.
--
-- 코드 쪽은 같은 commit 에서 핸들러·시드·테스트를 함께 지웠다. 스케줄러는 30초 폴링
-- 으로 잡 목록을 다시 읽으므로 이 행을 지우면 다음 폴링에 등록이 풀린다. 컨테이너
-- 재시작 불필요.

DELETE FROM scheduled_job_runs WHERE job_id = 'job_worker.cleanup_old_data';
DELETE FROM scheduled_jobs     WHERE id     = 'job_worker.cleanup_old_data';

-- 확인: 두 줄 모두 0 이어야 한다.
SELECT count(*) AS remaining_job  FROM scheduled_jobs     WHERE id     = 'job_worker.cleanup_old_data';
SELECT count(*) AS remaining_runs FROM scheduled_job_runs WHERE job_id = 'job_worker.cleanup_old_data';
