# `news_pipeline_kor/` — 국내 뉴스 파이프라인

Track E 소유. v2 포팅.

## v2 원본

`prime_jennie/services/news/` — app.py, collector.py, analyzer.py, archiver.py

## 파이프라인 (2026-04-21 이후 — v2 3-thread 구조 이식)

```
_collector_loop  : crawl (장중 10분 / 장외 30분 주기) → dedup → XADD v3:news:raw
_analyzer_loop   : XREADGROUP v3:news:analyzer (BLOCK 2s) → EXAONE 감성 → PG → XACK
_archiver_loop   : XREADGROUP v3:news:archiver (BLOCK 2s) → kure-v1 embed → Qdrant → XACK
```

3 async task 병렬 (`app.py`). Stream 이 단계 간 통로 → analyzer/archiver 가 상시 소비하므로
10분 burst 가 아닌 수 초 단위로 LLM/embed 호출이 분산 → GPU peak 평탄화.

초기 포팅(~2026-04-20)은 ``NewsPipeline.run_cycle`` 로 crawl+analyze+embed 를 10분 cron 에
한번에 몰았으나, GPU 팬 굉음의 근원이라 v2 원본 구조로 되돌림.

## Scout 공급

ticker별 최근 N시간 감성 평균 → `news_score` + RAG top-k
Latency 요구: 크롤링→Qdrant 5분 이내 (Scout 주기 1일 대비 안전)
