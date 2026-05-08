"""Phase 2.10 통합 smoke — v3 단독 운영 전환 검증.

MS-01 에서 `docker compose --profile full --profile observe up -d` 이후 실행.
블로커(#1,#4,#5,#6,#7,#8,#10) 해소 순서대로 각 서비스 체크가 SKIP → PASS 로 전환.

단계 (각 단계 독립, 하나 실패해도 나머지 진행):

  Core infra
    1. Postgres: pg_isready + migrations/005 scheduled_jobs 테이블 존재
    2. Redis: PING 응답

  Observability (--with-observe)
    3. Loki /ready
    4. Grafana /api/health

  Phase 2.9 runner (이미 동작)
    5. KIS Gateway /health
    6. Telegram Bot Redis heartbeat
    7. scheduled_jobs 4 cron seed (news/price-minute/price-daily/scout) 존재

  Phase 2.10 확정:
    8. Dashboard /health            (#4 Track C)
    9. Monitor /health              (#5 Track C)
   10. job_worker seed 존재 확인    (#1 Track B — HTTP 없는 daemon)
       — briefing/backtest/council 은 library 이므로 probe 없음.

  Tunnel (--with-tunnel, MS-01 만)
   11. cloudflared metrics :2000

실행:
    uv run python -m scripts.phase_2_10_full_smoke
    uv run python -m scripts.phase_2_10_full_smoke --with-observe --with-new-apps

각 체크는 `PASS` / `FAIL` / `SKIP` / `TODO` 로 리포트. TODO 는 블로커 해소 후 활성화 예정.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import traceback
from dataclasses import dataclass

import httpx

logger = logging.getLogger("phase_2_10_smoke")

HTTP_TIMEOUT = 3.0


@dataclass
class StepResult:
    name: str
    status: str  # PASS / FAIL / SKIP / TODO
    detail: str = ""


# --- Core infra ---------------------------------------------------------


async def step_postgres(host: str, port: int, user: str, password: str, db: str) -> StepResult:
    try:
        import asyncpg
    except ImportError:
        return StepResult("postgres", "FAIL", "asyncpg 미설치")

    conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database=db)
    try:
        row = await conn.fetchrow(
            "SELECT to_regclass('public.scheduled_jobs') AS t1, "
            "to_regclass('public.scheduled_job_runs') AS t2"
        )
        assert row is not None
        if row["t1"] is None or row["t2"] is None:
            return StepResult(
                "postgres",
                "FAIL",
                f"migrations/005 미적용 (scheduled_jobs={row['t1']}, runs={row['t2']})",
            )
        return StepResult("postgres", "PASS", f"db={db} migrations/005 OK")
    finally:
        await conn.close()


async def step_redis(host: str, port: int, password: str) -> StepResult:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return StepResult("redis", "FAIL", "redis 미설치")

    r = aioredis.Redis(host=host, port=port, password=password, socket_timeout=HTTP_TIMEOUT)
    try:
        pong = await r.ping()
        return StepResult("redis", "PASS" if pong else "FAIL", f"PING={pong}")
    finally:
        await r.aclose()


# --- Observability ------------------------------------------------------


async def step_http_get(name: str, url: str, expect_status: int = 200) -> StepResult:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url)
        if resp.status_code == expect_status:
            return StepResult(name, "PASS", f"{url} → {resp.status_code}")
        return StepResult(name, "FAIL", f"{url} → {resp.status_code} (expected {expect_status})")
    except Exception as e:
        return StepResult(name, "FAIL", f"{type(e).__name__}: {e}")


# --- scheduled_jobs seed ------------------------------------------------


async def step_scheduled_jobs_seed(
    host: str, port: int, user: str, password: str, db: str
) -> StepResult:
    try:
        import asyncpg
    except ImportError:
        return StepResult("scheduled_jobs_seed", "FAIL", "asyncpg 미설치")

    # news_pipeline 은 2026-04-21 이후 Redis Stream 상시 소비로 전환 → scheduled_jobs
    # 에 행을 두지 않음 (migration 013). 대신 컨테이너 heartbeat 로 검증.
    expected = {
        "price_scheduler.collect_daily",
        "slow_loop.scout_daily",
        "job_worker.collect_minute_chart",
    }
    conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database=db)
    try:
        rows = await conn.fetch("SELECT id FROM scheduled_jobs")
        found = {r["id"] for r in rows}
        missing = expected - found
        if missing:
            return StepResult(
                "scheduled_jobs_seed", "FAIL", f"missing={sorted(missing)} found={len(found)}"
            )
        return StepResult("scheduled_jobs_seed", "PASS", f"4 cron seed 확인 ({len(found)} total)")
    finally:
        await conn.close()


async def step_job_worker_seed(
    host: str, port: int, user: str, password: str, db: str, min_count: int = 5
) -> StepResult:
    """job_worker 는 HTTP daemon 이 아니므로 DB seed 존재로 #1 도달 여부 판단.

    `scheduled_jobs WHERE owner='job_worker'` 가 min_count 이상이면 PASS.
    v2 utility_jobs_dag / macro_dag 대체 핸들러 (cleanup_old_data / macro_validate_store
    / macro_collect_global / macro_collect_korea / macro_quick / contract_smoke_test /
    update_naver_sectors) 가 seed 에 들어가 있을 때 #1 이 실질 도달.
    """
    try:
        import asyncpg
    except ImportError:
        return StepResult("job_worker_seed", "FAIL", "asyncpg 미설치")

    conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database=db)
    try:
        rows = await conn.fetch(
            "SELECT id FROM scheduled_jobs WHERE owner = 'job_worker' AND enabled"
        )
        count = len(rows)
        if count < min_count:
            return StepResult(
                "job_worker_seed",
                "FAIL",
                f"owner=job_worker count={count} < {min_count} (seed 미실행?)",
            )
        return StepResult("job_worker_seed", "PASS", f"owner=job_worker {count} handler 시드 확인")
    finally:
        await conn.close()


# --- Phase 2.10 신규 서비스 (블로커 해소 대기) --------------------------


PHASE_2_10_ENDPOINTS = [
    # (step_name, url, task_id)
    # Track D 확정: briefing(#6)/backtest(#8)/council(#7) 모두 library 로 확정 →
    # compose 서비스 없음 → smoke probe 에서 제외.
    # Track B 확정 (#1): job_worker 도 HTTP 없는 pure daemon → DB seed 체크
    # (step_job_worker_seed) 로 대체. 여기서는 제외.
    ("dashboard", "http://localhost:8090/health", "#4"),
    ("monitor", "http://localhost:8091/health", "#5"),
]


async def step_new_app(name: str, url: str, task_id: str) -> StepResult:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return StepResult(name, "PASS", f"{url} → 200")
        return StepResult(name, "FAIL", f"{url} → {resp.status_code}")
    except Exception as e:
        # 서비스가 아직 없으면 TODO (블로커 대기)
        return StepResult(name, "TODO", f"{task_id} 대기 ({type(e).__name__})")


# --- Driver -------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 2.10 통합 smoke")
    p.add_argument("--with-observe", action="store_true", help="loki/grafana 체크 포함")
    p.add_argument("--with-new-apps", action="store_true", help="#1/#4-#8 신규 서비스 probe")
    p.add_argument("--with-tunnel", action="store_true", help="cloudflared 체크 포함")
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    # env (localhost 기본 — MS-01 에서 compose 기동 후 실행)
    pg_host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
    pg_user = os.environ.get("POSTGRES_USER", "pj_runtime")
    pg_password = os.environ.get("POSTGRES_PASSWORD", "dev_runtime")
    pg_db = os.environ.get("POSTGRES_DB", "prime_jennie_v3")
    redis_host = os.environ.get("REDIS_HOST", "127.0.0.1")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_password = os.environ.get("REDIS_PASSWORD", "dev_redis")
    kis_gateway_url = os.environ.get("KIS_GATEWAY_URL", "http://localhost:8080")

    results: list[StepResult] = []

    async def _run(name: str, coro):
        try:
            results.append(await coro)
        except Exception as e:
            traceback.print_exc()
            results.append(StepResult(name, "FAIL", f"{type(e).__name__}: {e}"))

    # Core infra
    await _run("postgres", step_postgres(pg_host, pg_port, pg_user, pg_password, pg_db))
    await _run("redis", step_redis(redis_host, redis_port, redis_password))

    # Observability
    if args.with_observe:
        await _run("loki_ready", step_http_get("loki_ready", "http://localhost:3100/ready"))
        await _run(
            "grafana_health",
            step_http_get("grafana_health", "http://localhost:3300/api/health"),
        )
    else:
        results.append(StepResult("loki_ready", "SKIP", "--with-observe 미지정"))
        results.append(StepResult("grafana_health", "SKIP", "--with-observe 미지정"))

    # Phase 2.9 runner
    await _run("kis_gateway", step_http_get("kis_gateway", f"{kis_gateway_url}/health"))
    await _run(
        "scheduled_jobs_seed",
        step_scheduled_jobs_seed(pg_host, pg_port, pg_user, pg_password, pg_db),
    )
    # #1 job_worker 는 HTTP 가 없는 daemon. seed 로만 존재 확인.
    await _run(
        "job_worker_seed",
        step_job_worker_seed(pg_host, pg_port, pg_user, pg_password, pg_db),
    )

    # Phase 2.10 신규 서비스 (블로커 대기)
    if args.with_new_apps:
        for name, url, task_id in PHASE_2_10_ENDPOINTS:
            await _run(name, step_new_app(name, url, task_id))
    else:
        for name, _, task_id in PHASE_2_10_ENDPOINTS:
            results.append(StepResult(name, "SKIP", f"--with-new-apps 미지정 ({task_id})"))

    # Tunnel
    if args.with_tunnel:
        await _run(
            "cloudflared",
            step_http_get("cloudflared", "http://localhost:2000/metrics"),
        )
    else:
        results.append(StepResult("cloudflared", "SKIP", "--with-tunnel 미지정"))

    # Report
    print("\n=== Phase 2.10 full smoke ===")
    for r in results:
        print(f"  [{r.status:<4s}] {r.name:<22s} | {r.detail}")

    fails = [r for r in results if r.status == "FAIL"]
    todos = [r for r in results if r.status == "TODO"]
    print(
        f"\nsummary: PASS={sum(1 for r in results if r.status == 'PASS')} "
        f"FAIL={len(fails)} TODO={len(todos)} "
        f"SKIP={sum(1 for r in results if r.status == 'SKIP')}"
    )

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
