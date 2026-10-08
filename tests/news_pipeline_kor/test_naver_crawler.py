"""네이버 크롤러 adapter 테스트 — fixture JSON/HTML + respx.

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
    _parse_news_json,
    _redirect_target,
)
from prime_jennie_runtime.news_pipeline_kor.crawler import NewsCrawler

_NEWS_API_RE = r"https://m\.stock\.naver\.com/api/news/stock/\d+.*"


def _item(
    title: str,
    *,
    office: str = "001",
    article: str = "0000000001",
    dt: str = "202604160930",
    press: str = "연합뉴스",
    with_url: bool = True,
) -> dict:
    item = {
        "id": office + article,
        "officeId": office,
        "articleId": article,
        "officeName": press,
        "datetime": dt,
        "type": 1,
        "title": title,
        "titleFull": title,
        "body": "요약...",
    }
    if with_url:
        item["mobileNewsUrl"] = f"https://n.news.naver.com/mnews/article/{office}/{article}"
    return item


# 2026-10 실측 구조: 기사 묶음 리스트, 묶음마다 items. 네 번째 묶음은 빈 제목.
_SAMPLE_JSON = [
    {"total": 1, "items": [_item("삼성전자 분기 실적 서프라이즈", article="0000000001")]},
    {
        "total": 2,
        "items": [
            _item("증권가 &quot;적극 매수&quot;", article="0000000002", with_url=False),
        ],
    },
    {"total": 1, "items": [_item("특징주 삼성전자 상승", article="0000000003")]},
    {"total": 1, "items": [_item("", article="0000000004")]},
]


def test_parse_news_json_extracts_valid_articles():
    articles = _parse_news_json(_SAMPLE_JSON, "005930")
    # 빈 제목 항목 스킵. 노이즈 필터는 크롤러 레벨이라 여기선 3건.
    assert len(articles) == 3
    titles = [a.title for a in articles]
    assert "삼성전자 분기 실적 서프라이즈" in titles
    assert "특징주 삼성전자 상승" in titles


def test_parse_news_json_unescapes_html_entities():
    articles = _parse_news_json(_SAMPLE_JSON, "005930")
    assert '증권가 "적극 매수"' in [a.title for a in articles]


def test_parse_news_json_builds_url_when_missing():
    articles = _parse_news_json(_SAMPLE_JSON, "005930")
    a = next(a for a in articles if a.title.startswith("증권가"))
    assert str(a.source_url) == f"{N_NEWS_ARTICLE_BASE}/001/0000000002"


def test_parse_news_json_non_list_returns_empty():
    assert _parse_news_json({"error": "x"}, "005930") == []
    assert _parse_news_json([{"items": None}, "junk"], "005930") == []


def test_parse_news_json_bad_date_falls_back_to_now():
    [article] = _parse_news_json([{"items": [_item("hi", dt="invalid")]}], "005930")
    assert article.published_at is not None  # now fallback


def test_parse_news_json_date_is_treated_as_kst():
    """네이버가 주는 시각 문자열은 KST 로컬 시각. UTC 로 변환해 저장되어야.

    regression: 2026-04-21 이전 버그 — `dt.replace(tzinfo=UTC)` 로 KST 를 UTC
    로 잘못 태깅해 9 시간 앞 shift 된 채 저장. UI 에서 미래 시각으로 표시됨.
    """
    from datetime import UTC

    [article] = _parse_news_json([{"items": [_item("x", dt="202604211215")]}], "005930")
    # 12:15 KST == 03:15 UTC
    assert article.published_at.tzinfo is UTC
    assert article.published_at.hour == 3
    assert article.published_at.minute == 15


def test_article_id_is_deterministic_by_url():
    from prime_jennie_runtime.news_pipeline_kor.dedup import article_fingerprint

    [article] = _parse_news_json([{"items": [_item("Same", office="023", article="9")]}], "1")
    assert article.article_id == article_fingerprint(f"{N_NEWS_ARTICLE_BASE}/023/9")


# ---------- crawler 단위: respx 로 네이버 엔드포인트 mock ----------


@pytest.mark.asyncio
async def test_crawler_implements_protocol():
    crawler = NaverNewsCrawler(client=httpx.AsyncClient())
    assert isinstance(crawler, NewsCrawler)
    await crawler.client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_crawler_filters_noise_titles():
    async with respx.mock() as mock:
        mock.get(url__regex=_NEWS_API_RE).mock(return_value=httpx.Response(200, json=_SAMPLE_JSON))
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=1, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
        await client.aclose()

    # "특징주" 는 노이즈 필터로 제거 → 2 건
    assert len(articles) == 2
    assert all("특징주" not in a.title for a in articles)


@pytest.mark.asyncio
async def test_crawler_multi_page_fetches_each():
    async with respx.mock() as mock:
        route = mock.get(url__regex=_NEWS_API_RE).mock(
            return_value=httpx.Response(200, json=_SAMPLE_JSON)
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=3, request_delay_s=0.0)
        await crawler.crawl(["005930"])
        await client.aclose()

    # 3 페이지 호출, 쪽 번호가 1·2·3 으로 올라간다
    assert route.call_count == 3
    pages = [c.request.url.params["page"] for c in route.calls]
    assert pages == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_crawler_stops_on_empty_page():
    responses = iter([_SAMPLE_JSON, []])

    async with respx.mock() as mock:
        route = mock.get(url__regex=_NEWS_API_RE).mock(
            side_effect=lambda request: httpx.Response(200, json=next(responses))
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=5, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
        await client.aclose()

    # 빈 쪽을 만나면 더 넘기지 않는다
    assert route.call_count == 2
    assert len(articles) == 2


@pytest.mark.asyncio
async def test_crawler_http_error_returns_partial():
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json=_SAMPLE_JSON)
        return httpx.Response(410, text="gone")

    async with respx.mock() as mock:
        mock.get(url__regex=_NEWS_API_RE).mock(side_effect=_side_effect)
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=3, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
        await client.aclose()

    # 1 페이지 정상 수집, 2 페이지 410 으로 break → 1 페이지 분 (노이즈 제거 후 2건)
    assert len(articles) == 2


@pytest.mark.asyncio
async def test_crawler_bad_json_returns_empty():
    async with respx.mock() as mock:
        mock.get(url__regex=_NEWS_API_RE).mock(
            return_value=httpx.Response(200, text="<html>moved</html>")
        )
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client, max_pages=2, request_delay_s=0.0)
        articles = await crawler.crawl(["005930"])
        await client.aclose()
    assert articles == []


@pytest.mark.asyncio
async def test_crawler_timeout_error_moves_to_next_ticker():
    async with respx.mock() as mock:
        mock.get(url__regex=r"https://m\.stock\.naver\.com/api/news/stock/005930.*").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )
        mock.get(url__regex=r"https://m\.stock\.naver\.com/api/news/stock/000660.*").mock(
            return_value=httpx.Response(200, json=_SAMPLE_JSON)
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
async def test_crawler_no_client_creates_and_closes_own():
    """client=None 이면 자체 AsyncClient 를 열고 닫는다. respx 가 mock 하므로 누수 X."""
    async with respx.mock() as mock:
        mock.get(url__regex=_NEWS_API_RE).mock(return_value=httpx.Response(200, json=_SAMPLE_JSON))
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


def test_redirect_target_accepts_n_news_url():
    src = f"{N_NEWS_ARTICLE_BASE}/023/0003977888"
    assert _redirect_target(src) == src


def test_redirect_target_none_for_external_url():
    assert _redirect_target("https://example.com/external") is None


def test_redirect_target_none_for_legacy_news_read_url():
    # 옛 finance.naver.com 기사 주소는 이제 410 이라 본문 요청을 하지 않는다
    src = (
        "https://finance.naver.com/item/news_read.naver"
        "?article_id=0003977888&office_id=023&code=005930"
    )
    assert _redirect_target(src) is None


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
        src = f"{N_NEWS_ARTICLE_BASE}/023/0003977888"
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
        src = f"{N_NEWS_ARTICLE_BASE}/023/0003977888"
        body = await crawler.fetch_body(src)
        await client.aclose()

    assert body == ""


@pytest.mark.asyncio
async def test_fetch_body_graceful_on_connect_error():
    async with respx.mock(base_url="https://n.news.naver.com") as mock:
        mock.get("/mnews/article/023/0003977888").mock(side_effect=httpx.ConnectError("refused"))
        client = httpx.AsyncClient()
        crawler = NaverNewsCrawler(client=client)
        src = f"{N_NEWS_ARTICLE_BASE}/023/0003977888"
        body = await crawler.fetch_body(src)
        await client.aclose()

    assert body == ""


@pytest.mark.asyncio
async def test_fetch_body_skips_non_n_news_url():
    """외부 URL 은 요청 없이 즉시 빈 본문 — respx route 미등록이라 호출 시 에러날 것."""
    client = httpx.AsyncClient()
    crawler = NaverNewsCrawler(client=client)
    body = await crawler.fetch_body("https://example.com/external")
    await client.aclose()
    assert body == ""
