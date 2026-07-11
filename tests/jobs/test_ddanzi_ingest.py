"""딴지 증시 요약 크롤러 + 수집 잡 스모크 — HTTP 모킹 + fake pool."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.ddanzi import (
    _find_recent_srls,
    _parse_title_date,
    fetch_recent_summaries,
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

# 같은 글(srl 900000002)이 제목 앵커·본문 앵커 둘로 잡히는 상황.
_DUP_LIST_HTML = """
<html><body>
  <a href="/index.php?mid=free&document_srl=900000002">📊 2026년 6월 25일 미국 증시 요약</a>
  <a href="/free/900000002">📊 2026년 6월 25일 미국 증시 요약</a>
  <a href="/index.php?mid=free&document_srl=899999999">📊 2026년 6월 24일 미국 증시 요약</a>
</body></html>
"""

_BODY_HTML = """
<html><body><div class="xe_content">💬 마이크론 +15.7% 급등 vs 애플 -6.1% 급락
다우 ▲+0.14% / 나스닥 ▼-0.46% / S&amp;P500 ▼-0.01%
🇰🇷 한국 증시 전망: 반도체 강세 유지 기대</div></body></html>
"""


def test_parse_title_date():
    assert _parse_title_date("📊 2026년 6월 25일 미국 증시 요약") == date(2026, 6, 25)
    assert _parse_title_date("제목에 날짜 없음") is None


def test_find_recent_srls_picks_matches_per_market():
    us = _find_recent_srls(_LIST_HTML, "US", 5)
    kr = _find_recent_srls(_LIST_HTML, "KR", 5)
    assert us == [
        ("900000002", "📊 2026년 6월 25일 미국 증시 요약"),
        ("899999999", "📊 2026년 6월 24일 미국 증시 요약"),
    ]
    assert kr == [("900000001", "📊 2026년 6월 25일 한국 증시 마감 요약")]


def test_find_recent_srls_respects_limit():
    us = _find_recent_srls(_LIST_HTML, "US", 1)
    assert us == [("900000002", "📊 2026년 6월 25일 미국 증시 요약")]


def test_find_recent_srls_dedupes_same_srl():
    us = _find_recent_srls(_DUP_LIST_HTML, "US", 5)
    assert us == [
        ("900000002", "📊 2026년 6월 25일 미국 증시 요약"),
        ("899999999", "📊 2026년 6월 24일 미국 증시 요약"),
    ]


def test_find_recent_srls_empty_when_no_match():
    assert _find_recent_srls("<html><body>아무 글 없음</body></html>", "US", 5) == []


@pytest.mark.asyncio
async def test_fetch_recent_summaries_us_returns_multiple():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get(url__regex=_POST_RE).respond(200, text=_BODY_HTML)
        async with httpx.AsyncClient() as client:
            summaries = await fetch_recent_summaries(client, "US", limit=5)
    assert [s.document_srl for s in summaries] == ["900000002", "899999999"]
    assert all(s.market == "US" for s in summaries)
    assert summaries[0].summary_date == date(2026, 6, 25)
    assert all("마이크론" in s.body and "한국 증시 전망" in s.body for s in summaries)


@pytest.mark.asyncio
async def test_fetch_recent_summaries_skips_empty_body_but_continues():
    # 첫 글(900000002)은 본문 있음, 둘째 글(899999999)은 본문 없음 → 첫째만 남는다.
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get("https://www.ddanzi.com/free/900000002").respond(200, text=_BODY_HTML)
        mock.get("https://www.ddanzi.com/free/899999999").respond(
            200, text="<html><body>no content div</body></html>"
        )
        async with httpx.AsyncClient() as client:
            summaries = await fetch_recent_summaries(client, "US", limit=5)
    assert [s.document_srl for s in summaries] == ["900000002"]


@pytest.mark.asyncio
async def test_fetch_recent_summaries_empty_when_list_has_no_match():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text="<html><body>없음</body></html>")
        async with httpx.AsyncClient() as client:
            summaries = await fetch_recent_summaries(client, "US", limit=5)
    assert summaries == []


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
async def test_ddanzi_ingest_upserts_recent_with_source_tag():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get(url__regex=_POST_RE).respond(200, text=_BODY_HTML)
        async with httpx.AsyncClient() as client:
            result = await ddanzi_ingest(pool, client, market="US")

    # 최근 미국 글 2개(6-25·6-24)를 다 upsert — 놓친 날 자동 백필.
    assert result["found"] is True
    assert result["checked"] == 2
    assert result["upserted"] == 2
    assert result["document_srl"] == "900000002"
    inserts = [c for c in pool.conn.execute_calls if "global_macro_news_articles" in c[0]]
    assert len(inserts) == 2
    for _, args in inserts:
        # (article_id, source, feed_name, title, description, source_url, published_at)
        assert args[1] == "ddanzi_kimkr"
        assert args[2] == "ddanzi_us_market_summary"
        assert "미국 증시 요약" in args[3]
        assert "마이크론" in args[4]


@pytest.mark.asyncio
async def test_ddanzi_ingest_respects_limit():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text=_LIST_HTML)
        mock.get(url__regex=_POST_RE).respond(200, text=_BODY_HTML)
        async with httpx.AsyncClient() as client:
            result = await ddanzi_ingest(pool, client, market="US", limit=1)
    assert result["checked"] == 1
    assert result["upserted"] == 1
    assert result["document_srl"] == "900000002"


@pytest.mark.asyncio
async def test_ddanzi_ingest_graceful_when_no_post():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_LIST_RE).respond(200, text="<html><body>없음</body></html>")
        async with httpx.AsyncClient() as client:
            result = await ddanzi_ingest(pool, client, market="US")
    assert result == {"found": False, "upserted": 0, "checked": 0}
    assert pool.conn.execute_calls == []
