-- 015_kst_published_at_fix.sql
-- 2026-04-21 timezone 버그 보정.
--
-- 증상: News 페이지에 기사 시각이 약 9 시간 앞 (미래) 으로 표시됨. 예: 한국시간
-- 12:15 에 크롤된 기사가 UI 에 21:15 로 나타남.
--
-- 원인: news_pipeline_kor/adapters/naver_crawler.py::_parse_published_at 가
-- 네이버의 KST 시각 문자열을 `dt.replace(tzinfo=UTC)` 로 잘못 태깅. TIMESTAMPTZ
-- 내부적으로 UTC 저장이므로 UI 가 UTC→KST 변환 시 다시 +9h 더해져 총 +9h 미래로
-- 표시됨. 크롤러 코드는 같은 commit 에서 ``dt.replace(tzinfo=KST).astimezone(UTC)``
-- 로 수정.
--
-- 보정: 본 migration 시점 이전에 수집된 news_articles.published_at 과
-- news_events.published_at 을 모두 `-9 hours` shift. fetched_at / analyzed_at
-- 은 ``datetime.now(UTC)`` / ``datetime.now()`` 혼재라 별도 조사 필요 (이번
-- migration 은 published_at 만 보정).
--
-- idempotent 하지 않으므로 **한 번만** 실행. 본 파일이 실행되면 `configs` 테이블
-- 에 guard row 를 남김.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM configs WHERE config_key = 'migration_015_applied'
    ) THEN
        RAISE NOTICE 'migration 015 already applied, skipping';
    ELSE
        UPDATE news_articles
        SET published_at = published_at - INTERVAL '9 hours'
        WHERE published_at IS NOT NULL;

        UPDATE news_events
        SET published_at = published_at - INTERVAL '9 hours'
        WHERE published_at IS NOT NULL;

        INSERT INTO configs (config_key, config_value, description)
        VALUES (
            'migration_015_applied',
            NOW()::text,
            '2026-04-21 published_at KST→UTC tagging bug fix applied'
        );

        RAISE NOTICE 'migration 015: news_articles + news_events published_at shifted -9h';
    END IF;
END $$;
