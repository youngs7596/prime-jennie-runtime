"""네이버 금융 종목 뉴스 async 크롤러.

v2 ``prime_jennie.infra.crawlers.naver.crawl_stock_news`` (385줄) 의 핵심 로직을
``httpx.AsyncClient`` 로 포팅. v3 ``NewsCrawler`` Protocol 구현체.

유지한 것 (v2 와 동일):
- ``Referer`` 헤더 + 표준 UA
- 노이즈 제목 필터링 (시황/특징주 등)

바꾼 것 (v3):
- sync httpx → ``httpx.AsyncClient`` (주입 가능)
- ticker 리스트 일괄 처리 (universe)
- NewsArticle 모델은 v3 ``news_pipeline_kor.models.NewsArticle``
- article_id 는 ``article_fingerprint(article_url)`` (dedup 호환)
- in-memory ``_seen_hashes`` 제거 — dedup 책임은 파이프라인

2026-10 출처 교체: 네이버 금융 이전으로 옛 ``finance.naver.com/item/news_news.naver``
HTML 목록이 2026-09-17 저녁부터 410 을 돌려줘 3 주간 뉴스가 0 건이었다. 새 화면이
읽는 JSON(``m.stock.naver.com/api/news/stock/{code}``)으로 옮겼다. 기사 주소는 이제
n.news 원문 주소이고, 상세 본문은 그 주소에서 바로 받는다.

주의: 새 목록의 제목은 40 자 남짓에서 "..." 로 잘려 온다 (전체 제목 필드도 같다).
HTML 기호(``&quot;``)도 섞여 와서 풀어 둔다.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
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

# 종목 뉴스 목록 JSON. 쪽마다 기사 묶음(같은 사건 기사들)이 최신순으로 온다.
NAVER_NEWS_API = "https://m.stock.naver.com/api/news/stock"
NAVER_REFERER = "https://stock.naver.com/"
# 목록의 시각 문자열("202610081252")은 KST 로컬 시각이다.
_DATE_FORMAT = "%Y%m%d%H%M"

# 기사 상세 본문은 n.news 원문 페이지에서 받는다. 목록이 원문 주소를 바로 주므로
# 그 주소를 기사 주소로 저장하고 본문도 거기서 가져온다.
N_NEWS_HOST = "n.news.naver.com"
N_NEWS_ARTICLE_BASE = "https://n.news.naver.com/mnews/article"
# n.news 기사 본문 컨테이너 (네이버 뉴스 표준 마크업).
_BODY_SELECTOR = "#dic_area"
# 본문 저장 상한. extractor 는 400자 발췌만 쓰지만 news_articles.body 보존용 여유.
BODY_MAX_CHARS = 2000


@dataclass
class NaverNewsCrawler:
    """네이버 금융 종목 뉴스 크롤러. ``NewsCrawler`` Protocol 구현.

    ``client``: 주입 가능 (테스트는 respx transport). None 이면 자동 생성.
    """

    max_pages: int = 2
    page_size: int = 10
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
        headers = {"User-Agent": NAVER_UA, "Referer": NAVER_REFERER}
        url = f"{NAVER_NEWS_API}/{ticker}"
        for page in range(1, self.max_pages + 1):
            params = {"pageSize": str(self.page_size), "page": str(page)}
            try:
                resp = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                logger.warning("naver crawl %s p%d failed: %s", ticker, page, e)
                break

            if resp.status_code != 200:
                logger.warning("naver crawl %s p%d status=%d", ticker, page, resp.status_code)
                break

            try:
                data = resp.json()
            except ValueError as e:
                logger.warning("naver crawl %s p%d bad json: %s", ticker, page, e)
                break

            parsed = _parse_news_json(data, ticker)
            # 노이즈 제목 컷
            articles.extend(a for a in parsed if not self._is_noise(a.title))
            if not parsed:
                break

            if page < self.max_pages:
                await asyncio.sleep(self.request_delay_s)
        return articles

    def _is_noise(self, title: str) -> bool:
        return any(kw in title for kw in self.noise_keywords)

    async def fetch_body(self, source_url: str) -> str:
        """기사 상세 본문을 best-effort 로 반환. 실패 시 빈 문자열.

        n.news 원문 주소가 아니면 (옛 ``news_read.naver`` 주소·외부 URL 등) 요청 없이
        빈 문자열. HTTP 오류·비200·파싱 실패도 모두 빈 본문 폴백 — 본문을 못 가져와도
        뉴스 파이프라인은 헤드라인-only 로 정상 동작한다.
        """
        target = _redirect_target(source_url)
        if target is None:
            return ""

        client, owns = self._ensure_client()
        try:
            resp = await client.get(
                target,
                headers={"User-Agent": NAVER_UA},
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            logger.warning("naver body fetch %s failed: %s", target, e)
            return ""
        finally:
            if owns:
                await client.aclose()

        if resp.status_code != 200:
            logger.warning("naver body fetch %s status=%d", target, resp.status_code)
            return ""
        return _parse_article_body(resp.content)


# ---------------------------------------------------------------------
# 파싱 (pure) — 테스트 fixture 로 HTML 고정 후 검증 쉽도록 분리
# ---------------------------------------------------------------------


def _parse_news_json(data: Any, ticker: str) -> list[NewsArticle]:
    """종목 뉴스 목록 JSON 을 NewsArticle 리스트로.

    응답은 기사 묶음의 리스트이고 묶음마다 ``items`` 에 기사가 들어 있다. 구조가
    다르거나 기사 주소를 만들 수 없는 항목은 건너뛴다.
    """
    if not isinstance(data, list):
        return []

    out: list[NewsArticle] = []
    for cluster in data:
        items = cluster.get("items") if isinstance(cluster, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            headline = _collapse_whitespace(
                html.unescape(str(item.get("titleFull") or item.get("title") or ""))
            )
            if not headline:
                continue
            article_url = _article_url(item)
            if not article_url:
                continue
            press = str(item.get("officeName") or "").strip()
            out.append(
                NewsArticle(
                    article_id=article_fingerprint(article_url),
                    ticker=ticker,
                    title=headline,
                    body="",  # 상세 본문은 파이프라인이 fetch_body 로 따로 채운다
                    published_at=_parse_published_at(str(item.get("datetime") or "")),
                    source_url=article_url,
                    source_name=press or "NAVER",
                )
            )
    return out


def _article_url(item: dict) -> str:
    """기사 원문 주소. ``mobileNewsUrl`` 이 없으면 언론사·기사 번호로 만든다."""
    url = str(item.get("mobileNewsUrl") or "").strip()
    if url.startswith("http"):
        return url
    office_id = str(item.get("officeId") or "").strip()
    article_id = str(item.get("articleId") or "").strip()
    if office_id and article_id:
        return f"{N_NEWS_ARTICLE_BASE}/{office_id}/{article_id}"
    return ""


def _redirect_target(source_url: str) -> str | None:
    """본문을 받을 n.news 기사 주소. n.news 원문 주소가 아니면 None — 호출부가
    본문 fetch 를 건너뛴다."""
    try:
        parsed = urlparse(source_url)
    except ValueError:
        return None
    if parsed.netloc != N_NEWS_HOST or not parsed.path.startswith("/mnews/article/"):
        return None
    return source_url


def _parse_article_body(body: bytes) -> str:
    """n.news.naver.com 기사 페이지 HTML → 본문 텍스트.

    표준 본문 컨테이너(``#dic_area``)만 추출하고 script/style 은 제거. 컨테이너가
    없으면 빈 문자열 (구조 변경·비기사 페이지). ``BODY_MAX_CHARS`` 로 truncate.
    """
    soup = BeautifulSoup(body, "html.parser")
    container = soup.select_one(_BODY_SELECTOR)
    if container is None:
        return ""
    for tag in container.select("script, style"):
        tag.decompose()
    text = _collapse_whitespace(container.get_text(" ", strip=True))
    return text[:BODY_MAX_CHARS]


def _parse_published_at(date_str: str) -> datetime:
    """네이버 뉴스 목록의 시각 문자열(`%Y%m%d%H%M`)은 **KST 로컬 시각**이다.

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


__all__ = ["NAVER_NOISE_KEYWORDS", "NaverNewsCrawler"]
