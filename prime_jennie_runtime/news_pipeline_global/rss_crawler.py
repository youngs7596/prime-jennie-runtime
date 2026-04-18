"""feedparser wrapper — 여러 RSS 피드를 병렬로 긁어 GlobalNewsArticle 리스트로 정규화.

feedparser 는 순수 Python sync 라 `asyncio.to_thread` 로 감싼다.
피드 1개 실패는 전체를 막지 않는다 (로그 + 빈 리스트).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .feeds import RssFeed
from .models import GlobalNewsArticle

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "prime-jennie-runtime/0.1 (+https://github.com/youngs7596)"


def _article_id(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]


def _parse_published(entry: Any) -> datetime:
    """RSS 가 주는 날짜 필드 중 가장 믿을 만한 것을 UTC aware datetime 으로 변환."""
    # feedparser 는 `published_parsed` (time.struct_time) 를 만들어 주지만 실패할 때가 있다.
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is not None:
        try:
            return datetime(*parsed[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            pass
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except (TypeError, ValueError):
            pass
    return datetime.now(tz=UTC)


def _entry_to_article(entry: Any, feed: RssFeed) -> GlobalNewsArticle | None:
    link = getattr(entry, "link", None)
    title = getattr(entry, "title", None)
    if not link or not title:
        return None
    description = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    return GlobalNewsArticle(
        article_id=_article_id(link),
        source=feed.source,
        feed_name=feed.feed_name,
        title=title.strip(),
        description=description.strip(),
        source_url=link,
        published_at=_parse_published(entry),
    )


async def _fetch_feed_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=20.0)
    resp.raise_for_status()
    return resp.content


def _parse_feed_bytes(data: bytes) -> list[Any]:
    # feedparser 는 bytes 도 받는다. 파싱이 CPU-bound 여도 본문 작아서 to_thread 로 분리.
    import feedparser

    parsed = feedparser.parse(data)
    return list(getattr(parsed, "entries", []) or [])


async def fetch_one(client: httpx.AsyncClient, feed: RssFeed) -> list[GlobalNewsArticle]:
    try:
        data = await _fetch_feed_bytes(client, feed.url)
    except httpx.HTTPError as exc:
        logger.warning("rss fetch error feed=%s err=%s", feed.feed_name, exc)
        return []
    try:
        entries = await asyncio.to_thread(_parse_feed_bytes, data)
    except Exception as exc:  # feedparser 는 거의 안 던지지만 안전망
        logger.warning("rss parse error feed=%s err=%s", feed.feed_name, exc)
        return []
    out: list[GlobalNewsArticle] = []
    for e in entries:
        art = _entry_to_article(e, feed)
        if art is not None:
            out.append(art)
    return out


async def crawl_all(
    feeds: tuple[RssFeed, ...], client: httpx.AsyncClient | None = None
) -> list[GlobalNewsArticle]:
    """피드 리스트 전체를 병렬로 긁고 dedupe (article_id 기준, 첫 발견 우선)."""
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        assert client is not None
        results = await asyncio.gather(
            *(fetch_one(client, f) for f in feeds), return_exceptions=True
        )
    finally:
        if own:
            await client.aclose()  # type: ignore[union-attr]

    seen: set[str] = set()
    out: list[GlobalNewsArticle] = []
    for res in results:
        if isinstance(res, BaseException):
            logger.warning("rss gather error: %s", res)
            continue
        for art in res:
            if art.article_id in seen:
                continue
            seen.add(art.article_id)
            out.append(art)
    return out
