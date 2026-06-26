"""딴지일보 자유게시판 증시 요약 크롤러 — 정대만mitsui(아이디 hisasimitsui, 김경록 PB).

매 영업일 두 번 올라오는 미국/한국 증시 요약 글을 작성자 닉 검색으로 찾아 본문을 받는다.
무로그인 공개 글(UTF-8). 설계: `.ai/designs/2026-06-26-ddanzi-market-summary-ingest.md`.

발견은 현재 닉(`정대만mitsui`)으로 잡는다 — 옛 아이디 `hisasimitsui` 는 닉 변경 전 글까지만
검색돼서 일별 요약을 못 본다(2026-06-26 PoC 확인). 닉이 또 바뀌면 글목록이 안 잡히므로,
향후 member_srl 글목록으로 강건화할 여지를 남긴다(설계 참고).

제목 패턴(실측): `📊 YYYY년 M월 D일 미국 증시 요약` / `... 한국 증시 마감 요약`.
본문 컨테이너: `div.xe_content`. 검색 리스트는 최신순이라 첫 매칭이 가장 최근 글이다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DDANZI_BASE = "https://www.ddanzi.com"
DDANZI_SEARCH_URL = f"{DDANZI_BASE}/index.php"
DDANZI_AUTHOR_NICK = "정대만mitsui"

DDANZI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# market → 제목에 모두 들어가야 할 키워드. US 와 KR 은 서로 교차 매칭되지 않는다
# (미국 요약엔 '한국' 없고, 한국 요약엔 '미국' 없음).
_TITLE_KEYS: dict[str, tuple[str, ...]] = {
    "US": ("미국", "증시", "요약"),
    "KR": ("한국", "증시", "요약"),
}

_DATE_RE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_SRL_RE = re.compile(r"document_srl=(\d+)")


@dataclass(frozen=True)
class DdanziSummary:
    document_srl: str
    title: str
    market: str  # US | KR
    summary_date: date | None  # 제목에서 파싱한 요약 대상일 (게시일과 다를 수 있음)
    url: str
    body: str


def _parse_title_date(title: str) -> date | None:
    m = _DATE_RE.search(title)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _find_latest_srl(list_html: str, market: str) -> tuple[str, str] | None:
    """작성자 검색 리스트에서 해당 market 의 가장 최근 (document_srl, title). 없으면 None."""
    keys = _TITLE_KEYS[market]
    soup = BeautifulSoup(list_html, "html.parser")
    for a in soup.select("a[href*='document_srl=']"):
        title = a.get_text(strip=True)
        if not title or not all(k in title for k in keys):
            continue
        srl = _SRL_RE.search(a.get("href", ""))
        if srl:
            return srl.group(1), title  # 리스트 최신순 → 첫 매칭이 최신
    return None


def _extract_body(post_html: str) -> str:
    soup = BeautifulSoup(post_html, "html.parser")
    el = soup.select_one("div.xe_content")
    return el.get_text("\n", strip=True) if el else ""


async def fetch_latest_summary(client: httpx.AsyncClient, market: str) -> DdanziSummary | None:
    """market('US'|'KR')의 최신 증시 요약 글을 받아 DdanziSummary 반환. 실패·없음이면 None.

    실패(네트워크/파싱)는 None 으로 흘려 호출측이 graceful skip 하게 한다.
    """
    if market not in _TITLE_KEYS:
        raise ValueError(f"unknown market: {market}")
    params = {
        "mid": "free",
        "search_target": "nick_name",
        "search_keyword": DDANZI_AUTHOR_NICK,
    }
    try:
        resp = await client.get(
            DDANZI_SEARCH_URL, params=params, headers=DDANZI_HEADERS, timeout=15.0
        )
        resp.raise_for_status()
        found = _find_latest_srl(resp.text, market)
        if found is None:
            logger.warning("ddanzi: no %s summary in author list", market)
            return None
        srl, title = found

        url = f"{DDANZI_BASE}/free/{srl}"
        post = await client.get(url, headers=DDANZI_HEADERS, timeout=15.0)
        post.raise_for_status()
        body = _extract_body(post.text)
        if not body:
            logger.warning("ddanzi: empty body for %s (srl=%s)", market, srl)
            return None

        return DdanziSummary(
            document_srl=srl,
            title=title,
            market=market,
            summary_date=_parse_title_date(title),
            url=url,
            body=body,
        )
    except Exception as e:
        logger.warning("ddanzi fetch failed (%s): %s", market, e)
        return None


__all__ = [
    "DDANZI_AUTHOR_NICK",
    "DdanziSummary",
    "fetch_latest_summary",
]
