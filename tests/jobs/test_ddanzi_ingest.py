"""딴지 증시 요약 크롤러 + 수집 잡 스모크 — HTTP 모킹 + fake pool."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.ddanzi import (
    _find_latest_srl,
    _parse_title_date,
    fetch_latest_summary,
)
from prime_jennie_runtime.jobs.ddanzi_ingest import ddanzi_ingest

_LIST_RE = r"https://www\.ddanzi\.com/index\.php.*"
_POST_RE = r"https://www\.ddanzi\.com/free/\d+"

# 검색 리스트(최신순): 미국 6-25 → 한국 6-25 → 미국 6-24.
_LIST_HTML = """
<html><body><table class="title">
  <a href="/index.php?mid=free&document_srl=900000002">
     <span style="white-space: pre-wrap;">📊 2026년 6월 25일 미국 증시 요약</span></a>
  <a href="/index.php?mid=free&document_srl=900000001">
     <span style="white-space: pre-wrap;">📊 2026년 6월 25일 한국 증시 마감 요약</span></a>
  <a href="/index.php?mid=free&document_srl=899999999">
     <span style="white-space: pre-wrap;">📊 2026년 6월 24일 미국 증시 요약</span></a>
</table></body></html>
"""

_BODY_HTML = """
<html><body><div class="xe_content">💬 마이크론 +15.7% 급등 vs 애플 -6.1% 급락
다우 ▲+0.14% / 나스닥 ▼-0.46% / S&amp;P500 ▼-0.01%
🇰🇷 한국 증시 전망: 반도체 강세 유지 기대</div></body></html>
"""


def test_parse_title_date():
    assert _parse_title_date("📊 2026년 6월 25일 미국 증시 요약") == date(2026, 6, 25)
    assert _parse_title_date("제목에 날짜 없음") is None


def test_find_latest_srl_picks_first_match_per_market():
    us = _find_latest_srl(_LIST_HTML, "US")
    kr = _find_latest_srl(_LIST_HTML, "KR")
    assert us == ("900000002", "📊 2026년 6월 25일 미국 증시 요약")
    assert kr == ("900000001", "📊 2026년 6월 25일 한국 증시 마감 요약")


def test_find_latest_srl_none_when_no_match():
    assert _find_latest_srl("<html><body>아무 글 없음</body></html>", "US") is None


@pytest.mark.asyncio
async def test_fetch_latest_summary_us():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get(url__regex=_POST_RE).respond(200, text=_BODY_HTML)
        async with httpx.AsyncClient() as client:
            s = await fetch_latest_summary(client, "US")
    assert s is not None
    assert s.document_srl == "900000002"
    assert s.market == "US"
    assert s.summary_date == date(2026, 6, 25)
    assert s.url == "https://www.ddanzi.com/free/900000002"
    assert "마이크론" in s.body and "한국 증시 전망" in s.body


@pytest.mark.asyncio
async def test_fetch_latest_summary_empty_body_returns_none():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get(url__regex=_POST_RE).respond(200, text="<html><body>no content div</body></html>")
        async with httpx.AsyncClient() as client:
            s = await fetch_latest_summary(client, "US")
    assert s is None


class _FakeConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _T()


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_ddanzi_ingest_upserts_with_source_tag():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get(url__regex=_POST_RE).respond(200, text=_BODY_HTML)
        async with httpx.AsyncClient() as client:
            result = await ddanzi_ingest(pool, client, market="US")

    assert result == {"found": True, "upserted": 1, "document_srl": "900000002"}
    inserts = [c for c in pool.conn.execute_calls if "global_macro_news_articles" in c[0]]
    assert len(inserts) == 1
    args = inserts[0][1]
    # (article_id, source, feed_name, title, description, source_url, published_at)
    assert args[1] == "ddanzi_kimkr"
    assert args[2] == "ddanzi_us_market_summary"
    assert "미국 증시 요약" in args[3]
    assert "마이크론" in args[4]
    assert args[5] == "https://www.ddanzi.com/free/900000002"


@pytest.mark.asyncio
async def test_ddanzi_ingest_graceful_when_no_post():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text="<html><body>없음</body></html>")
        async with httpx.AsyncClient() as client:
            result = await ddanzi_ingest(pool, client, market="US")
    assert result == {"found": False, "upserted": 0}
    assert pool.conn.execute_calls == []
