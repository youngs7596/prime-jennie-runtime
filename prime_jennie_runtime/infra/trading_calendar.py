"""한국 거래일 가드 — kis-gateway HTTP 경유 KIS chk-holiday 사용.

slow_loop / job_worker 핸들러가 휴장일에도 cron 으로 도는 사고 (2026-05-25)
재발 방지용 진입부 가드. cron 자체에 휴장 캘린더를 박는 대신, 실행 직전에
gateway 의 `is-market-open` 응답 session 이 'holiday' 인지 확인한다.

주말은 HTTP 호출 없이 즉시 False. 평일은 일 단위 process-local 캐시.
HTTP 실패 시 True (거래일 가정) — gateway 일시 장애가 데이터 누락으로
번지는 걸 막기 위한 보수 fallback. kis_gateway/market_hours.py 의
MarketCalendar 와 같은 정책.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

_KST = timezone(timedelta(hours=9))
_cache: dict[str, bool] = {}

logger = logging.getLogger(__name__)


def _reset_cache_for_testing() -> None:
    _cache.clear()


async def is_trading_day_via_gateway(
    gateway_url: str,
    *,
    http: httpx.AsyncClient | None = None,
) -> bool:
    """오늘이 한국 거래일인지 판정. 주말 + 한국 휴장 모두 False.

    Args:
        gateway_url: kis-gateway base URL (예: ``http://kis-gateway:8080``).
        http: 재사용할 httpx.AsyncClient. None 이면 일회용 client 생성.
            jobs/app.py 처럼 long-lived client 가 있으면 주입 권장.
    """
    today_kst = datetime.now(_KST).date()
    if today_kst.weekday() >= 5:
        return False

    key = today_kst.isoformat()
    if key in _cache:
        return _cache[key]

    url = f"{gateway_url.rstrip('/')}/api/market/is-market-open"
    try:
        if http is None:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
        else:
            resp = await http.get(url, timeout=5.0)
        resp.raise_for_status()
        session = (resp.json() or {}).get("session", "")
        is_trading = session != "holiday"
    except Exception:
        logger.warning("trading-day check failed via %s — assuming trading day", url, exc_info=True)
        return True

    _cache[key] = is_trading
    if not is_trading:
        logger.info("Non-trading day (holiday): %s", key)
    return is_trading


__all__ = ["is_trading_day_via_gateway"]
