# `news_pipeline_kor/` — 국내 뉴스 파이프라인

Track E 소유. v2 포팅.

## v2 원본

`prime_jennie/services/news/` — app.py, collector.py, analyzer.py, archiver.py

## 파이프라인

```
네이버 금융 뉴스 크롤러 (10분 주기)
  → 중복 제거
    → EXAONE 4.0 Q8 감성분석 (-1.0 ~ +1.0)
      → kure-v1 임베딩
        → Qdrant 저장
```

## Scout 공급

ticker별 최근 N시간 감성 평균 → `news_score` + RAG top-k
Latency 요구: 크롤링→Qdrant 5분 이내 (Scout 주기 1일 대비 안전)
