"""네이버 금융 모바일 JSON API 공용 접근 — 2026-09 사이트 이전 대응.

네이버가 2026-09-10 무렵 `finance.naver.com` 의 HTML 화면을 `stock.naver.com`
으로 옮겼다. 옛 주소는 302 만 돌려주고 본문이 없어 HTML 파싱 크롤러가 전부
빈손으로 돌아왔다 (잡은 계속 success 로 기록돼 9-10 이후 PER/PBR·ROE·종목별
수급·섹터 매핑이 조용히 멈췄다).

새 화면이 실제로 읽는 JSON API 는 `m.stock.naver.com/api` 에 있고 로그인·토큰이
필요 없다. HTML 셀렉터 대신 이쪽을 읽는다. 주소가 또 바뀔 때 고칠 곳을 한 군데로
모으려고 base URL 과 헤더를 여기에 둔다.

시장 전체 투자자 수급은 모바일 API 에 없고 새 웹 화면(`stock.naver.com/api`)에만
있다 (2026-09-16 무렵 옛 `investorDealTrendDay` 가 410 으로 닫힘). 그래서 웹 쪽
base URL 도 여기에 같이 둔다.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NAVER_API_BASE = "https://m.stock.naver.com/api"
NAVER_WEB_API_BASE = "https://stock.naver.com/api"

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://stock.naver.com/",
}


async def get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
    base: str = NAVER_API_BASE,
) -> Any | None:
    """`m.stock.naver.com/api` 의 한 경로를 읽어 파싱된 JSON 을 돌려준다.
    웹 API 를 읽을 때는 ``base=NAVER_WEB_API_BASE``.

    실패(네트워크·비 200·JSON 아님)면 None. 옛 HTML 크롤러가 실패를 None 으로
    돌려주던 규약을 그대로 유지해 호출측 skip 카운트 처리가 안 바뀌게 한다.
    """
    url = f"{base}{path}"
    try:
        resp = await client.get(url, params=params, headers=NAVER_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("GET %s -> HTTP %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as e:
        logger.warning("GET %s failed: %s", url, e)
        return None


def parse_number(raw: Any) -> float | None:
    """API 가 돌려주는 숫자 문자열("-1,088,039", "46.55%", "+251,835")을 float 로.

    값 없음을 뜻하는 "-", "", None 은 None 으로 돌려준다.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in ("-", "N/A"):
        return None
    text = text.replace(",", "").replace("%", "").replace("+", "")
    text = text.replace("−", "-").replace("–", "-")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


__all__ = [
    "NAVER_API_BASE",
    "NAVER_HEADERS",
    "NAVER_WEB_API_BASE",
    "get_json",
    "parse_number",
]
