"""News 라우터 happy path — news_events + news_articles 조회.

SQLite 테스트라 ARRAY/ANY() 는 생략. 단일 필터 경로 검증 + 상세/stats.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _seed(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO stock_masters (stock_code, stock_name, market, is_active) "
                "VALUES ('005930', '삼성전자', 'KOSPI', 1), "
                "       ('000660', 'SK하이닉스', 'KOSPI', 1)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO news_articles "
                "(article_id, ticker, title, body, published_at, source_url, source_name) "
                "VALUES ('a1', '005930', '삼성전자 1분기 어닝서프라이즈', "
                "        '영업이익 7조원 달성', :t, 'https://example.com/1', '연합'),"
                "       ('a2', '000660', 'SK하이닉스 HBM 수주', "
                "        'TSMC 대형 계약', :t, 'https://example.com/2', '한경')"
            ),
            {"t": now},
        )
        await session.execute(
            text(
                "INSERT INTO news_events "
                "(article_id, ticker, published_at, event_type, impact_level, sentiment, "
                " sentiment_score, time_horizon, keywords, sector_tags, financial_signals, "
                " confidence, model, analyzed_at) "
                "VALUES "
                "('a1', '005930', :t, 'earnings', 'high', 'positive', 0.85, 'short', "
                ' \'["삼성전자","영업이익"]\', \'["반도체"]\', '
                ' \'[{"type":"revenue","direction":"up"}]\', 0.92, \'qwen3\', :t),'
                "('a2', '000660', :t, 'contract', 'medium', 'positive', 0.5, 'medium', "
                ' \'["HBM","TSMC"]\', \'["반도체"]\', '
                " '[]', 0.8, 'qwen3', :t)"
            ),
            {"t": now},
        )
        await session.commit()


async def test_list_events_default(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/events?since_hours=48")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        tickers = {it["ticker"] for it in body["items"]}
        assert tickers == {"005930", "000660"}
        # high impact 가 먼저 정렬
        assert body["items"][0]["ticker"] == "005930"
        assert body["items"][0]["event_type"] == "earnings"
        assert body["items"][0]["impact_level"] == "high"
        assert body["items"][0]["stock_name"] == "삼성전자"


async def test_list_events_ticker_filter(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/events?ticker=005930&since_hours=48")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["ticker"] == "005930"


async def test_list_events_keyword_filter(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/events?keyword=어닝서프라이즈&since_hours=48")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["article_id"] == "a1"


async def test_list_events_since_window(app, session_factory):
    """오래된 뉴스는 since_hours 밖으로 제외."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO news_articles (article_id, ticker, title, published_at) "
                "VALUES ('old', '005930', '오래된 뉴스', :old)"
            ),
            {"old": now - timedelta(hours=72)},
        )
        await session.execute(
            text(
                "INSERT INTO news_events "
                "(article_id, ticker, published_at, event_type, impact_level, sentiment, "
                " sentiment_score, analyzed_at) "
                "VALUES ('old', '005930', :old, 'other', 'low', 'neutral', 0.0, :old)"
            ),
            {"old": now - timedelta(hours=72)},
        )
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/events?since_hours=24")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


async def test_get_event_detail(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/events/a1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["article_id"] == "a1"
        assert body["body"] == "영업이익 7조원 달성"
        assert body["model"] == "qwen3"
        assert isinstance(body["financial_signals"], list)


async def test_get_event_not_found(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/events/unknown")
        assert resp.status_code == 404


async def test_news_stats(app, session_factory):
    await _seed(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/news/stats?since_hours=48&top_n=5")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_events"] == 2
        et_types = {e["event_type"]: e for e in body["event_types"]}
        assert set(et_types.keys()) == {"earnings", "contract"}
        assert et_types["earnings"]["high_impact"] == 1
        assert et_types["earnings"]["positive"] == 1
        # top_tickers 는 count 동률이라 순서 상관없이 둘 다 있어야
        top_codes = {t["ticker"] for t in body["top_tickers"]}
        assert top_codes == {"005930", "000660"}
