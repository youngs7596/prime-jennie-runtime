FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 의존성 캐시 레이어
COPY pyproject.toml /app/

# minyoung-mah 로컬 설치 (빌드 시 COPY)
COPY minyoung-mah/ /opt/minyoung-mah/
RUN pip install --no-cache-dir /opt/minyoung-mah && \
    pip install --no-cache-dir -e .

# 소스 복사
COPY prime_jennie_runtime/ /app/prime_jennie_runtime/
COPY migrations/ /app/migrations/

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

CMD ["uvicorn", "prime_jennie_runtime.slow_loop:app", "--host", "0.0.0.0", "--port", "8080"]
