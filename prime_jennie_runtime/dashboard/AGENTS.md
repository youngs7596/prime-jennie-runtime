# dashboard/ — v3 Dashboard 백엔드 (Track C, Phase 2.10)

v2 `prime_jennie/services/dashboard/` 포팅. **재작성 금지, 어댑터 교체만**.

## 핵심 차이 (v2 → v3)

| 영역 | v2 | v3 |
|---|---|---|
| DB | MariaDB + SQLModel sync | Postgres 16 + SQLAlchemy 2.0 async (`infra.db`) |
| 스케줄러 | Airflow 3 REST | apscheduler + `scheduled_jobs` 테이블 CRUD (migrations/005) |
| Redis | sync `redis.Redis` | async `redis.asyncio.Redis` |
| CORS | 로컬 only | `DASHBOARD_CORS_ORIGINS` env |

## 구조

```
dashboard/
  app.py          # FastAPI app factory + lifespan + CORS
  deps.py         # FastAPI Depends: AsyncSession / aioredis.Redis
  routers/
    airflow.py    # scheduled_jobs/scheduled_job_runs CRUD (v2 Airflow 대체)
    llm_stats.py  # Redis llm:stats:{date}:{svc} 조회 (v2 키 포맷 유지)
    logs.py       # Loki proxy (env LOKI_URL)
    macro.py      # macro_runs 테이블 조회
    portfolio.py  # executions/outcomes + KIS balance (v3 kis_gateway)
    system.py     # v3 서비스 헬스 체크
    trades.py     # executions 조회
    watchlist.py  # Redis + position_sheets 조회
```

## 포팅 규칙

- 각 라우터 파일 상단에 v2 원본 경로 명시
- pydantic 모델/응답 스키마는 최대한 v2와 동일 (frontend 계약 유지)
- 테이블이 다른 경우 (예: `trades` → `executions`) 필드 매핑 명시
- 테스트: `tests/dashboard/routers/test_*.py` happy path 1 개 이상
