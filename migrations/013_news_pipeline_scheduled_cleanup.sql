-- 013_news_pipeline_scheduled_cleanup.sql
-- news-pipeline 을 cron 기반 crawl_cycle handler 에서 Redis Stream 상시 소비 모델
-- (v2 collector/analyzer/archiver 3-thread 포팅) 로 전환. 관련 scheduled_jobs
-- row 를 제거한다. 이 migration 이후 `news_pipeline` owner 는 scheduler 에 등록되지
-- 않고, `news-pipeline` 컨테이너가 자체 asyncio loop 로 주기적 수집 + stream 상시
-- 소비를 담당한다.
--
-- idempotent: 없어도 에러 나지 않음.

DELETE FROM scheduled_jobs WHERE owner = 'news_pipeline';
