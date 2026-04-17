"""v3 job-worker — v2 `prime_jennie/services/jobs/app.py` 대체.

v2 FastAPI 엔드포인트 26 개 중 4 개는 이미 v3 runner 로 이관됨 (news_pipeline,
price_scheduler, slow_loop.scout_daily). 나머지 22 개는 여기로 포팅한다.

- 파이썬 함수 = apscheduler 호출 가능한 `async def handler(**kwargs)`.
- 도메인 별로 파일 분리: fundamentals / market_data / analytics / maintenance /
  council_macro.
- 진입점은 `prime_jennie_runtime/jobs/app.py` (job-worker runner) 가 owner
  `job_worker` 의 scheduled_jobs 를 모두 적재해 실행한다.
"""
