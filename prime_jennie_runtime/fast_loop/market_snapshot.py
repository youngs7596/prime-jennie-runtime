"""Runtime market snapshot fetcher — `run_risk_updater` 용 5분 주기 데이터 소스.

세 곳에서 읽어 `(kospi_pct, vix, sox_pct, council_mult)` 튜플로 합친다.

- KOSPI 일중 등락률: 네이버 모바일 지수 API (`fetch_index_data`)
- VIX / SOX: Redis `macro:data:snapshot:{YYYY-MM-DD}` (jobs/council_macro 가 발행)
- council_mult: PG `macro_runs.size_multiplier` 가장 최근 gate=open 행

각 소스가 실패해도 throttle 평가가 멈추지 않도록 fail-open 기본값을 돌려준다
(KOSPI=0.0, VIX=None, SOX=None, council=1.0).
"""

from __future__ import annotations

import json
import logging
from datetime import date

import asyncpg
import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.jobs.council_macro import MACRO_SNAPSHOT_KEY_PREFIX
from prime_jennie_runtime.jobs.crawlers.naver_market import fetch_index_data

logger = logging.getLogger(__name__)


class RuntimeMarketSnapshotFetcher:
    """`run_risk_updater` 의 fetch_snapshot callable."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        redis_client: aioredis.Redis,
        pool: asyncpg.Pool,
    ) -> None:
        self._http = http
        self._redis = redis_client
        self._pool = pool

    async def __call__(self) -> tuple[float, float | None, float | None, float]:
        kospi_pct = await self._fetch_kospi_pct()
        vix, sox_pct = await self._fetch_macro_overnight()
        council_mult = await self._fetch_council_mult()
        return kospi_pct, vix, sox_pct, council_mult

    async def _fetch_kospi_pct(self) -> float:
        try:
            idx = await fetch_index_data(self._http, "KOSPI")
            if idx is None:
                return 0.0
            return float(idx.change_pct)
        except Exception:
            logger.exception("kospi index fetch failed")
            return 0.0

    async def _fetch_macro_overnight(self) -> tuple[float | None, float | None]:
        try:
            key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{date.today().isoformat()}"
            raw = await self._redis.get(key)
            if raw is None:
                return None, None
            data = json.loads(raw if isinstance(raw, (str, bytes, bytearray)) else str(raw))
            vix_raw = data.get("vix")
            sox_raw = data.get("sox_change_pct")
            vix = float(vix_raw) if vix_raw is not None else None
            sox_pct = float(sox_raw) if sox_raw is not None else None
            return vix, sox_pct
        except Exception:
            logger.exception("macro snapshot read failed")
            return None, None

    async def _fetch_council_mult(self) -> float:
        try:
            row = await self._pool.fetchrow(
                "SELECT size_multiplier FROM macro_runs "
                "WHERE gate = 'open' "
                "ORDER BY generated_at DESC LIMIT 1"
            )
            if row is None:
                return 1.0
            return float(row["size_multiplier"])
        except Exception:
            logger.exception("council_mult fetch failed")
            return 1.0


__all__ = ["RuntimeMarketSnapshotFetcher"]
