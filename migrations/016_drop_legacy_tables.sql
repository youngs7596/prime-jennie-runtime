-- 016_drop_legacy_tables.sql
-- v2 잔존 dead tables 일괄 drop. v3 운영 경로는 더 이상 이 테이블들을 사용하지
-- 않거나 (daily_macro_insights / global_macro_snapshots) v2 frozen snapshot 으로
-- 보존하던 분석용 테이블 (legacy_*) 을 v3 outcomes / scout 기반 분석으로
-- 대체했음.
--
-- drop 대상 (총 6 테이블):
--   - daily_macro_insights      (008): v2 거시 일일 판단 — v3 macro_runs 가 대체
--   - global_macro_snapshots    (008): v2 거시 지표 일일 스냅샷 — v3 미사용 (0 row)
--   - legacy_signal_logs        (002): v2 signal_logs 아카이브 — 운영 코드에서 미참조
--   - legacy_trade_logs         (002): v2 trade_logs 아카이브 — analytics 잡 제거와 함께 정리
--   - legacy_quant_scores       (002): v2 daily_quant_scores 아카이브 — briefing watchlist 빈 리스트로 대체
--   - legacy_news_sentiments    (010): v2 stock_news_sentiments 아카이브 — 운영 코드에서 미참조
--
-- 운영 코드 변경 (별도 PR 에 함께 포함):
--   - prime_jennie_runtime/jobs/analytics.py: analyze_ai_performance / analyst_feedback 제거
--   - prime_jennie_runtime/jobs/app.py: 두 job handler 등록 해제
--   - prime_jennie_runtime/briefing/context_builder.py: _collect_watchlist 가 빈 리스트 반환
--   - scripts/seed_scheduled_jobs.py: 두 scheduled_jobs 시드 제거
--
-- idempotent: 없어도 에러 나지 않음 (IF EXISTS).

DROP TABLE IF EXISTS daily_macro_insights CASCADE;
DROP TABLE IF EXISTS global_macro_snapshots CASCADE;
DROP TABLE IF EXISTS legacy_signal_logs CASCADE;
DROP TABLE IF EXISTS legacy_trade_logs CASCADE;
DROP TABLE IF EXISTS legacy_quant_scores CASCADE;
DROP TABLE IF EXISTS legacy_news_sentiments CASCADE;

-- legacy_trade_logs 가 사라지면서 더 이상 의미 없는 두 잡 row 제거.
DELETE FROM scheduled_jobs WHERE id = 'job_worker.analyze_ai_performance';
DELETE FROM scheduled_jobs WHERE id = 'job_worker.analyst_feedback';
