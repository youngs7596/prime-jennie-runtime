"""Track B 전용 crawler 포팅.

v2 `prime_jennie/infra/crawlers/` 중 job-worker 에서 사용하는 함수만
v3 async httpx 스타일로 옮긴다. 뉴스 크롤링은 Track E
`news_pipeline_kor/adapters/naver_crawler.py` 에 이미 있어 중복 포팅하지
않는다.
"""
