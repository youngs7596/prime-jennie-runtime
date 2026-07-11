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


def _find_recent_srls(list_html: str, market: str, limit: int) -> list[tuple[str, str]]:
    """작성자 검색 리스트에서 해당 market 의 최근 (document_srl, title) 을 최신순으로 최대
    limit 개 돌려준다. 리스트가 최신순이라 앞에서부터 매칭하면 그대로 최신순이다. 같은 글이
    여러 앵커로 잡혀도 srl 로 한 번만 센다. 매칭이 없으면 빈 리스트."""
    keys = _TITLE_KEYS[market]
    soup = BeautifulSoup(list_html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='document_srl=']"):
        title = a.get_text(strip=True)
        if not title or not all(k in title for k in keys):
            continue
        m = _SRL_RE.search(a.get("href", ""))
        if not m:
            continue
        srl = m.group(1)
        if srl in seen:
            continue
        seen.add(srl)
        out.append((srl, title))
        if len(out) >= limit:
            break
    return out


def _extract_body(post_html: str) -> str:
    soup = BeautifulSoup(post_html, "html.parser")
    el = soup.select_one("div.xe_content")
    return el.get_text("\n", strip=True) if el else ""


DEFAULT_RECENT_LIMIT = 5


async def fetch_recent_summaries(
    client: httpx.AsyncClient, market: str, limit: int = DEFAULT_RECENT_LIMIT
) -> list[DdanziSummary]:
    """market('US'|'KR')의 최근 증시 요약 글들을 최신순으로 최대 limit 개 받아 반환.

    최신 한 건만 보던 옛 방식은 글이 07:00 이후·주말에 늦게 올라오면 그 글을 놓쳤다 —
    다음 실행이 더 새 글을 집으면서 그 사이 글이 영영 누락됐다. 최근 몇 개를 훑으면 upsert
    멱등과 맞물려 놓친 날이 다음 실행에서 저절로 메꿔진다. 개별 글 실패(본문 없음/네트워크)는
    건너뛰고 계속 진행한다. 리스트 조회 자체가 실패하면 빈 리스트.
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
    except Exception as e:
        logger.warning("ddanzi list fetch failed (%s): %s", market, e)
        return []

    found = _find_recent_srls(resp.text, market, limit)
    if not found:
        logger.warning("ddanzi: no %s summary in author list", market)
        return []

    summaries: list[DdanziSummary] = []
    for srl, title in found:
        url = f"{DDANZI_BASE}/free/{srl}"
        try:
            post = await client.get(url, headers=DDANZI_HEADERS, timeout=15.0)
            post.raise_for_status()
        except Exception as e:
            logger.warning("ddanzi post fetch failed (%s srl=%s): %s", market, srl, e)
            continue
        body = _extract_body(post.text)
        if not body:
            logger.warning("ddanzi: empty body for %s (srl=%s)", market, srl)
            continue
        summaries.append(
            DdanziSummary(
                document_srl=srl,
                title=title,
                market=market,
                summary_date=_parse_title_date(title),
                url=url,
                body=body,
            )
        )
    return summaries


__all__ = [
    "DDANZI_AUTHOR_NICK",
    "DEFAULT_RECENT_LIMIT",
    "DdanziSummary",
    "fetch_recent_summaries",
]
