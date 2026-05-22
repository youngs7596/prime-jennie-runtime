"""네이버 크롤러 adapter 테스트 — fixture HTML + respx.

실제 네트워크 호출은 하지 않는다. 파싱 / 노이즈 필터 / 에러 복구 를 단위 검증.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.news_pipeline_kor.adapters.naver_crawler import (
    BODY_MAX_CHARS,
    N_NEWS_ARTICLE_BASE,
    NaverNewsCrawler,
    _parse_article_body,
    _parse_news_page,
    _redirect_target,
)
from prime_jennie_runtime.news_pipeline_kor.crawler import NewsCrawler

# 네이버 금융 종목 뉴스 페이지의 핵심 마크업 (2024 기준, table.type5 tr 구조).
# euc-kr 실제 인코딩 대신 utf-8 로 저장 후 파서의 ``errors="replace"`` 로 처리.
_SAMPLE_HTML = """
<html><body>
<table class="type5">
<tr>
  <td class="title"><a href="/item/news_read.naver?id=1">삼성전자 분기 실적 서프라이즈</a></td>
  <td class="info">연합뉴스</td>
  <td class="date">2026.04.16 09:30</td>
</tr>
<tr>
  <td class="title"><a href="https://example.com/external">외부 기사</a></td>
  <td class="info">블룸버그</td>
  <td class="date">2026.04.16 10:00</td>
</tr>
<tr>
  <td class="title"><a href="/item/x">특징주 삼성전자 상승</a></td>
  <td class="info">이데일리</td>
  <td class="date">2026.04.16 10:15</td>
</tr>
<tr>
  <td class="title"><a href="/item/y"></a></td>
  <td class="info">공백제목</td>
  <td class="date">2026.04.16 10:20</td>
</tr>
</table>
</body></html>
""".encode()


def test_parse_news_page_extracts_valid_articles():
    articles = _parse_news_page(_SAMPLE_HTML, "005930")
    # 빈 제목 행 스킵. 노이즈 필터는 크롤러 레벨이라 여기선 3건.
    assert len(articles) == 3
    titles = [a.title for a in articles]
    assert "삼성전자 분기 실적 서프라이즈" in titles
    assert "외부 기사" in titles
    assert "특징주 삼성전자 상승" in titles


def test_parse_news_page_absolute_url_for_relative_href():
    articles = _parse_news_page(_SAMPLE_HTML, "005930")
    first = next(a for a in articles if a.title.startswith("삼성"))
    assert str(first.source_url).startswith("https://finance.naver.com/item/news_read.naver")


def test_parse_news_page_keeps_external_url():
    articles = _parse_news_page(_SAMPLE_HTML, "005930")
    ext = next(a for a in articles if a.title == "외부 기사")
    assert str(ext.source_url).startswith("https://example.com/")


def test_parse_news_page_missing_table_returns_empty():
    assert _parse_news_page(b"<html><body>no table</body></html>", "005930") == []


def test_parse_news_page_bad_date_falls_back_to_now():
    html = (
        b'<table class="type5"><tr>'
        b'<td class="title"><a href="/x">hi</a></td>'
        b'<td class="date">invalid</td></tr></table>'
    )
    [article] = _parse_news_page(html, "005930")
    assert article.published_at is not None  # now fallback


def test_parse_news_page_date_is_treated_as_kst():
    """네이버가 주는 날짜 문자열은 KST 로컬 시각. UTC 로 변환해 저장되어야.

    regression: 2026-04-21 이전 버그 — `dt.replace(tzinfo=UTC)` 로 KST 를 UTC
    로 잘못 태깅해 9 시간 앞 shift 된 채 저장. UI 에서 미래 시각으로 표시됨.
    """
    from datetime import UTC

    html = (
        b'<table class="type5"><tr>'
        b'<td class="title"><a href="/x">x</a></td>'
        b'<td class="date">2026.04.21 12:15</td></tr></table>'
    )
    [article] = _parse_news_page(html, "005930")
    # 12:15 KST == 03:15 UTC
    assert article.published_at.tzinfo is UTC
    assert article.published_at.hour == 3
    assert article.published_at.minute == 15


# ---------- crawler 단위: respx 로 네이버 엔드포인트 mock ----------


@pytest.mark.asyncio
async def test_crawler_implements_protocol():
    crawler = NaverNewsCrawler(client=httpx.AsyncClient())
    assert isinstance(crawler, NewsCrawler)
    await crawler.client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_crawler_filters_noise_titles():
    async with respx.mock(base_url="https://finance.naver.com") as mock:
        mock.get("/item/news_news.naver").mock(
            return_value=httpx.Response(200, content=_SAMPLE_HTML)
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=1, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
        await client.aclose()

    # "특징주" 는 노이즈 필터로 제거 → 2 건 (비-빈 제목 2개 중 노이즈 1개 제외)
    assert len(articles) == 2
    assert all("특징주" not in a.title for a in articles)


@pytest.mark.asyncio
async def test_crawler_multi_page_fetches_each():
    async with respx.mock(base_url="https://finance.naver.com") as mock:
        route = mock.get("/item/news_news.naver").mock(
            return_value=httpx.Response(200, content=_SAMPLE_HTML)
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=3, request_delay_s=0.0)
        await crawler.crawl(["005930"])
        await client.aclose()

    # 3 페이지 호출
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_crawler_http_error_returns_partial():
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, content=_SAMPLE_HTML)
        return httpx.Response(502, text="bad gateway")

    async with respx.mock(base_url="https://finance.naver.com") as mock:
        mock.get("/item/news_news.naver").mock(side_effect=_side_effect)
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=3, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
        await client.aclose()

    # 1 페이지 정상 수집, 2 페이지 502 로 break → 1 페이지 분 (노이즈 제거 후 2건)
    assert len(articles) == 2


@pytest.mark.asyncio
async def test_crawler_timeout_error_moves_to_next_ticker():
    async with respx.mock(base_url="https://finance.naver.com") as mock:
        mock.get("/item/news_news.naver", params={"code": "005930"}).mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )
        mock.get("/item/news_news.naver", params={"code": "000660"}).mock(
            return_value=httpx.Response(200, content=_SAMPLE_HTML)
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=1, request_delay_s=0.0)
        articles = await crawler.crawl(["005930", "000660"])
        await client.aclose()

    # 005930 은 timeout 으로 0건, 000660 은 2건
    tickers = [a.ticker for a in articles]
    assert "005930" not in tickers
    assert tickers.count("000660") == 2


@pytest.mark.asyncio
async def test_crawler_article_id_is_deterministic_by_url():
    from prime_jennie_runtime.news_pipeline_kor.dedup import article_fingerprint

    html = (
        b'<table class="type5"><tr><td class="title"><a href="/item/x">Same</a></td></tr></table>'
    )
    [article] = _parse_news_page(html, "005930")
    expected = article_fingerprint("https://finance.naver.com/item/x")
    assert article.article_id == expected


@pytest.mark.asyncio
async def test_crawler_no_client_creates_and_closes_own():
    """client=None 이면 자체 AsyncClient 를 열고 닫는다. respx 가 mock 하므로 누수 X."""
    async with respx.mock(base_url="https://finance.naver.com") as mock:
        mock.get("/item/news_news.naver").mock(
            return_value=httpx.Response(200, content=_SAMPLE_HTML)
        )
        crawler = NaverNewsCrawler(max_pages=1, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
    assert len(articles) >= 2


# ---------- 기사 상세 본문 fetch ----------

# n.news.naver.com 기사 페이지 핵심 마크업 (#dic_area 표준 컨테이너).
_ARTICLE_HTML = """
<html><head><meta charset="utf-8"></head><body>
<div id="ct">
  <article id="dic_area" class="go_trans _article_content">
    삼성전자가 1분기 영업이익 7조원을 기록했다.
    <script>sendTracking();</script>
    <style>.ad{color:red}</style>
    HBM 수요 폭발이 실적을 견인했다.
  </article>
</div>
</body></html>
""".encode()


def test_redirect_target_builds_n_news_url():
    src = (
        "https://finance.naver.com/item/news_read.naver"
        "?article_id=0003977888&office_id=023&code=005930&page=1&sm="
    )
    assert _redirect_target(src) == f"{N_NEWS_ARTICLE_BASE}/023/0003977888"


def test_redirect_target_none_for_external_url():
    assert _redirect_target("https://example.com/external") is None


def test_redirect_target_none_when_query_missing():
    # 목록 fallback URL (article_id/office_id 없음)
    assert _redirect_target("https://finance.naver.com/item/news.naver?code=005930") is None


def test_parse_article_body_extracts_text_without_scripts():
    body = _parse_article_body(_ARTICLE_HTML)
    assert "삼성전자가 1분기 영업이익 7조원을 기록했다." in body
    assert "HBM 수요 폭발이 실적을 견인했다." in body
    # script/style 내용은 제거
    assert "sendTracking" not in body
    assert "color:red" not in body


def test_parse_article_body_empty_when_no_container():
    assert _parse_article_body(b"<html><body>no article</body></html>") == ""


def test_parse_article_body_truncates_to_max():
    long_text = "가" * (BODY_MAX_CHARS + 500)
    html = f'<article id="dic_area">{long_text}</article>'.encode()
    assert len(_parse_article_body(html)) == BODY_MAX_CHARS


@pytest.mark.asyncio
async def test_fetch_body_returns_text_on_success():
    async with respx.mock(base_url="https://n.news.naver.com") as mock:
        mock.get("/mnews/article/023/0003977888").mock(
            return_value=httpx.Response(200, content=_ARTICLE_HTML)
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client)
        src = (
            "https://finance.naver.com/item/news_read.naver"
            "?article_id=0003977888&office_id=023&code=005930"
        )
        body = await crawler.fetch_body(src)
        await client.aclose()

    assert "삼성전자가 1분기 영업이익 7조원을 기록했다." in body


@pytest.mark.asyncio
async def test_fetch_body_graceful_on_http_error():
    async with respx.mock(base_url="https://n.news.naver.com") as mock:
        mock.get("/mnews/article/023/0003977888").mock(
            return_value=httpx.Response(502, text="bad gateway")
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client)
        src = "https://finance.naver.com/item/news_read.naver?article_id=0003977888&office_id=023"
        body = await crawler.fetch_body(src)
        await client.aclose()

    assert body == ""


@pytest.mark.asyncio
async def test_fetch_body_graceful_on_connect_error():
    async with respx.mock(base_url="https://n.news.naver.com") as mock:
        mock.get("/mnews/article/023/0003977888").mock(side_effect=httpx.ConnectError("refused"))
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client)
        src = "https://finance.naver.com/item/news_read.naver?article_id=0003977888&office_id=023"
        body = await crawler.fetch_body(src)
        await client.aclose()

    assert body == ""


@pytest.mark.asyncio
async def test_fetch_body_skips_non_news_read_url():
    """외부 URL 은 요청 없이 즉시 빈 본문 — respx route 미등록이라 호출 시 에러날 것."""
    client = httpx.AsyncClient()
    crawler = NaverNewsCrawler(client=client)
    body = await crawler.fetch_body("https://example.com/external")
    await client.aclose()
    assert body == ""
