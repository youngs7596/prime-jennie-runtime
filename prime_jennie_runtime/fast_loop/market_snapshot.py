"""Runtime market snapshot fetcher — `run_risk_updater` 용 5분 주기 데이터 소스.

두 곳에서 읽어 `(kospi_pct, vix, sox_pct)` 튜플로 합친다.

- KOSPI 일중 등락률: 네이버 모바일 지수 API (직접 호출 — bs4 미필요)
- VIX / SOX: Redis `macro:data:snapshot:{YYYY-MM-DD}` (jobs/council_macro 가 발행)

각 소스가 실패해도 throttle 평가가 멈추지 않도록 fail-open 기본값을 돌려준다
(KOSPI=0.0, VIX=None, SOX=None).

`jobs.crawlers.naver_market` 의 `fetch_index_data` 와 동일한 엔드포인트지만
fast-loop 이미지가 bs4 미포함이라 (해당 모듈 임포트 시 bs4 끌어옴) 의존성을
끊고 KOSPI 부분만 인라인했다.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import httpx
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_MACRO_SNAPSHOT_KEY_PREFIX = "macro:data:snapshot:"
_NAVER_INDEX_BASE = "https://m.stock.naver.com/api/index"
_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def fetch_kospi_change_pct(client: httpx.AsyncClient) -> float | None:
    """네이버 모바일 지수 API 에서 KOSPI 일중 등락률 (%)."""
    try:
        resp = await client.get(
            f"{_NAVER_INDEX_BASE}/KOSPI/basic", headers=_NAVER_HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data["fluctuationsRatio"])
    except Exception as e:
        logger.warning("KOSPI index fetch failed: %s", e)
        return None


class RuntimeMarketSnapshotFetcher:
    """`run_risk_updater` 의 fetch_snapshot callable."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        redis_client: aioredis.Redis,
    ) -> None:
        self._http = http
        self._redis = redis_client

    async def __call__(self) -> tuple[float, float | None, float | None]:
        kospi_pct = await self._fetch_kospi_pct()
        vix, sox_pct = await self._fetch_macro_overnight()
        return kospi_pct, vix, sox_pct

    async def _fetch_kospi_pct(self) -> float:
        pct = await fetch_kospi_change_pct(self._http)
        return pct if pct is not None else 0.0

    async def _fetch_macro_overnight(self) -> tuple[float | None, float | None]:
        try:
            key = f"{_MACRO_SNAPSHOT_KEY_PREFIX}{date.today().isoformat()}"
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


__all__ = ["RuntimeMarketSnapshotFetcher"]
