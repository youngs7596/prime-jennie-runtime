"""네이버 금융 종목 뉴스 async 크롤러.

v2 ``prime_jennie.infra.crawlers.naver.crawl_stock_news`` (385줄) 의 핵심 로직을
``httpx.AsyncClient`` 로 포팅. v3 ``NewsCrawler`` Protocol 구현체.

유지한 것 (v2 와 동일):
- ``Referer`` 헤더 (네이버 금융이 요구) + 표준 UA
- ``euc-kr`` 인코딩
- ``table.type5 tr`` 구조, ``tbody`` 미사용
- 노이즈 제목 필터링 (시황/특징주 등)
- 날짜 파싱 `%Y.%m.%d %H:%M`

바꾼 것 (v3):
- sync httpx → ``httpx.AsyncClient`` (주입 가능)
- ticker 리스트 일괄 처리 (universe)
- NewsArticle 모델은 v3 ``news_pipeline_kor.models.NewsArticle``
- article_id 는 ``article_fingerprint(article_url)`` (dedup 호환)
- in-memory ``_seen_hashes`` 제거 — dedup 책임은 파이프라인
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from ..dedup import article_fingerprint
from ..models import NewsArticle

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

NAVER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# v2 와 동일 (NOISE_KEYWORDS). 시황/특징주 등 투자 판단에 무의미한 제목 필터.
NAVER_NOISE_KEYWORDS: tuple[str, ...] = (
    "특징주",
    "오전 시황",
    "장마감",
    "마감 시황",
    "급등락",
    "오늘의 증시",
    "환율",
    "개장",
    "출발",
    "상위 종목",
    "단독",
    "인포",
    "증권리포트",
    "장중시황",
    "[이슈종합]",
    "인기 기업",
    "한줄리포트",
    "이 시각 증권",
)

_DATE_FORMAT = "%Y.%m.%d %H:%M"


@dataclass
class NaverNewsCrawler:
    """네이버 금융 종목 뉴스 크롤러. ``NewsCrawler`` Protocol 구현.

    ``client``: 주입 가능 (테스트는 respx transport). None 이면 자동 생성.
    """

    max_pages: int = 2
    request_delay_s: float = 0.3
    timeout_s: float = 10.0
    noise_keywords: tuple[str, ...] = field(default_factory=lambda: NAVER_NOISE_KEYWORDS)
    client: httpx.AsyncClient | None = None

    async def crawl(self, universe: list[str]) -> list[NewsArticle]:
        client, owns = self._ensure_client()
        try:
            all_articles: list[NewsArticle] = []
            for ticker in universe:
                articles = await self._crawl_ticker(client, ticker)
                all_articles.extend(articles)
            return all_articles
        finally:
            if owns:
                await client.aclose()

    # ----- internals -----

    def _ensure_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.client is not None:
            return self.client, False
        return httpx.AsyncClient(timeout=self.timeout_s), True

    async def _crawl_ticker(self, client: httpx.AsyncClient, ticker: str) -> list[NewsArticle]:
        articles: list[NewsArticle] = []
        headers = {
            "User-Agent": NAVER_UA,
            "Referer": f"https://finance.naver.com/item/news.naver?code={ticker}",
        }
        for page in range(1, self.max_pages + 1):
            url = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page={page}"
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as e:
                logger.warning("naver crawl %s p%d failed: %s", ticker, page, e)
                break

            if resp.status_code != 200:
                logger.warning("naver crawl %s p%d status=%d", ticker, page, resp.status_code)
                break

            parsed = _parse_news_page(resp.content, ticker)
            # 노이즈 제목 컷
            articles.extend(a for a in parsed if not self._is_noise(a.title))

            if page < self.max_pages:
                await asyncio.sleep(self.request_delay_s)
        return articles

    def _is_noise(self, title: str) -> bool:
        return any(kw in title for kw in self.noise_keywords)


# ---------------------------------------------------------------------
# 파싱 (pure) — 테스트 fixture 로 HTML 고정 후 검증 쉽도록 분리
# ---------------------------------------------------------------------


def _parse_news_page(body: bytes, ticker: str) -> list[NewsArticle]:
    """네이버 금융 종목 뉴스 페이지 HTML 을 NewsArticle 리스트로.

    encoding 은 BeautifulSoup(UnicodeDammit) 에 위임 — 실 네이버 응답은 euc-kr,
    테스트 fixture 는 utf-8 이어도 자동 감지.
    """
    soup = BeautifulSoup(body, "html.parser", from_encoding="euc-kr")
    # euc-kr 로 시도했는데 깨진 흔적(대체 문자)이 너무 많으면 utf-8 로 재파싱.
    if _looks_garbled(soup.get_text(" ", strip=True)):
        soup = BeautifulSoup(body, "html.parser", from_encoding="utf-8")

    table = soup.select_one("table.type5")
    if table is None:
        return []

    out: list[NewsArticle] = []
    for row in table.select("tr"):
        title_td = row.select_one("td.title")
        if title_td is None:
            continue
        link = title_td.select_one("a")
        if link is None:
            continue
        headline = link.get_text(strip=True)
        if not headline:
            continue

        href = link.get("href") or ""
        article_url = _absolute_url(href)

        press_td = row.select_one("td.info")
        press = press_td.get_text(strip=True) if press_td is not None else ""

        date_td = row.select_one("td.date")
        date_str = date_td.get_text(strip=True) if date_td is not None else ""
        published_at = _parse_published_at(date_str)

        out.append(
            NewsArticle(
                article_id=article_fingerprint(article_url or headline),
                ticker=ticker,
                title=_collapse_whitespace(headline),
                body="",  # 상세 본문은 별도 요청 필요 — Phase 2 scope 밖
                published_at=published_at,
                source_url=article_url
                or f"https://finance.naver.com/item/news.naver?code={ticker}",
                source_name=press or "NAVER",
            )
        )
    return out


def _absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://finance.naver.com{href}"
    return f"https://finance.naver.com/{href}"


def _parse_published_at(date_str: str) -> datetime:
    """네이버 금융 뉴스의 날짜 문자열(`%Y.%m.%d %H:%M`)은 **KST 로컬 시각**이다.

    TIMESTAMPTZ 컬럼은 UTC 로 정규화 저장되므로 KST 로 tz 태깅 후 UTC 로 변환해
    반환. 2026-04-21 이전에는 ``dt.replace(tzinfo=UTC)`` 로 잘못 태깅해 9 시간
    앞 shift 된 채 저장됐음 (UI 상 미래 시각으로 표시).
    """
    if date_str:
        try:
            dt = datetime.strptime(date_str, _DATE_FORMAT)
            return dt.replace(tzinfo=KST).astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


_WS_RE = re.compile(r"\s+")


def _collapse_whitespace(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _looks_garbled(text: str) -> bool:
    """replacement char(U+FFFD) 비율이 5%+ 면 인코딩 miss 로 간주."""
    if not text:
        return False
    bad = text.count("\ufffd")
    return bad > 0 and bad / max(len(text), 1) > 0.05


__all__ = ["NAVER_NOISE_KEYWORDS", "NaverNewsCrawler"]
