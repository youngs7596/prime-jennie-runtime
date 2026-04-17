"""slow_loop 한 사이클 수동 smoke — 실 vLLM EXAONE LLM 호출 검증.

scheduled_jobs 의 cron 을 건드리지 않고 run_slow_loop() 을 직접 호출.
성공 시: macro gate 평가 + (open 이면) scout 코드 생성 + strategy sheet 발행 흐름 로그.

실행:
    set -a; source .env; set +a
    uv run python -m scripts.smoke_slow_loop_once
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.slow_loop.app import _build_slow_loop_components
from prime_jennie_runtime.slow_loop.pipeline import run_slow_loop


async def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()
    redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
    engine = create_engine(cfg.postgres)
    try:
        comp = _build_slow_loop_components(redis_client, db_engine=engine)
        if comp is None:
            print("components unavailable — VLLM_LLM_URL/MODEL 또는 langchain_openai 누락")
            return 1

        now = datetime.now(tz=KST)
        suffix = uuid.uuid4().hex[:8]
        print(f"=== slow_loop smoke run @ {now.isoformat()} ===")
        result = await run_slow_loop(
            comp,
            as_of_date=now.date(),
            as_of_dt=now,
            macro_run_id=f"mr_smoke_{suffix}",
            scout_run_id=f"sr_smoke_{suffix}",
            macro_trigger="smoke",
            scout_trigger="smoke",
        )
        print("=== result ===")
        print(f"skipped_reason : {result.skipped_reason}")
        print(f"macro_post.gate: {result.macro_post.output.gate if result.macro_post else 'N/A'}")
        print(
            f"macro_post.size: "
            f"{result.macro_post.output.size_multiplier if result.macro_post else 'N/A'}"
        )
        if result.scout_output is not None:
            print(f"scout.hypothesis: {result.scout_output.hypothesis}")
            print(f"scout.expected  : {result.scout_output.expected_candidates}")
        print(f"sheets_published: {len(result.sheets_published)} ({result.sheets_published})")
        print(f"sheets_rejected : {len(result.sheets_rejected)}")
        return 0
    finally:
        await redis_client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
