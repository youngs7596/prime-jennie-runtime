"""CNBC 시장 크롤러 — VKOSPI(코스피200 변동성지수) 일별 시계열.

2026-06-24 출처 조사 결과: KRX 웹포털은 안티봇으로 막혀(LOGOUT) 있고 KRX OpenAPI 카탈로그엔
변동성지수 서비스가 없으며 네이버·야후도 VKOSPI 를 안 줘서, Linux 서버에서 httpx 로 안정
수집 가능한 일별 VKOSPI 출처는 CNBC 내부 차트 API 한 곳뿐이다. 인증·키·쿠키 불필요(UA
헤더만). 비공식 API 라 스키마/심볼 변경에 대비해 호출부에서 값 sanity 가드를 둔다.

range 토큰 이름이 실제 깊이·주기와 전혀 안 맞는다(2026-06-24 실측, 중앙간격 기준):
YTD/1M≈1년 일봉, 1Y/3M≈2년 일봉, 6M≈3년 일봉(729봉, 가장 깊은 일봉). **5Y 는 주봉,
ALL 은 월봉이라 일봉엔 부적합**(과거 백필은 6M 을 쓸 것). 평소 증분은 1M 으로 충분(둘 다
idempotent upsert). 심볼은 점(.)을 포함한 ``.KSVKOSPI`` — 점 누락 주의.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

logger = logging.getLogger(__name__)

VKOSPI_SYMBOL = ".KSVKOSPI"
_CHART_URL = "https://ts-api.cnbc.com/harmony/app/charts/{range_token}.json"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class VkospiBar:
    price_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float


async def fetch_vkospi_daily(client: httpx.AsyncClient, range_token: str = "5Y") -> list[VkospiBar]:
    """CNBC .KSVKOSPI 일별 OHLC. 오래된 순 정렬. 실패 시 빈 리스트."""
    url = _CHART_URL.format(range_token=range_token)
    try:
        resp = await client.get(url, headers=_HEADERS, params={"symbol": VKOSPI_SYMBOL}, timeout=15)
        resp.raise_for_status()
        bars_raw = resp.json()["barData"]["priceBars"]
    except Exception as e:
        logger.warning("CNBC VKOSPI fetch failed (%s): %s", range_token, e)
        return []

    bars: list[VkospiBar] = []
    for b in bars_raw:
        try:
            tt = str(b["tradeTime"])  # "YYYYMMDDhhmmss"
            price_date = date(int(tt[0:4]), int(tt[4:6]), int(tt[6:8]))
            bars.append(
                VkospiBar(
                    price_date=price_date,
                    open_price=float(b["open"]),
                    high_price=float(b["high"]),
                    low_price=float(b["low"]),
                    close_price=float(b["close"]),
                )
            )
        except (KeyError, ValueError, IndexError, TypeError) as e:
            logger.debug("VKOSPI bar parse skip: %s — %s", b, e)
            continue

    bars.sort(key=lambda x: x.price_date)
    logger.info("CNBC VKOSPI (%s): %d bars", range_token, len(bars))
    return bars


__all__ = ["VKOSPI_SYMBOL", "VkospiBar", "fetch_vkospi_daily"]
