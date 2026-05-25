"""`seed_stock_masters` 스모크 — 크롤러 monkeypatch + UPSERT 검증."""

from __future__ import annotations

import httpx
import pytest

from prime_jennie_runtime.jobs import stock_masters as smod
from prime_jennie_runtime.jobs.crawlers.naver_market import MarketStock


class _FakeConn:
    def __init__(self, existing: list[str]) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self._existing = existing

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetch_calls.append((sql, args))
        return [{"stock_code": c} for c in self._existing]

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, existing: list[str]) -> None:
        self.conn = _FakeConn(existing)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_seed_stock_masters_inserts_and_updates(monkeypatch):
    pool = _FakePool(existing=["005930"])  # 삼성전자만 기존, 카카오는 신규

    async def fake_market(client, market):
        return [
            MarketStock(stock_code="005930", stock_name="삼성전자", market_cap=400_000_000),
            MarketStock(stock_code="035720", stock_name="카카오", market_cap=20_000_000),
        ]

    async def fake_sectors(client):
        return {"005930": "전기전자", "035720": "서비스업"}

    monkeypatch.setattr(smod, "fetch_market_stocks", fake_market)
    monkeypatch.setattr(smod, "build_naver_sector_mapping", fake_sectors)

    async with httpx.AsyncClient() as client:
        result = await smod.seed_stock_masters(pool, client, market="KOSPI")

    assert result == {"inserted": 1, "updated": 1, "total": 2, "failed": 0}
    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_masters" in c[0]]
    assert len(inserts) == 2
    samsung = next(c for c in inserts if c[1][0] == "005930")
    assert samsung[1][2] == "KOSPI"
    assert samsung[1][3] == 400_000_000
    assert samsung[1][4] == "전기전자"


@pytest.mark.asyncio
async def test_seed_stock_masters_handles_missing_sector(monkeypatch):
    pool = _FakePool(existing=[])

    async def fake_market(client, market):
        return [MarketStock(stock_code="999999", stock_name="UnknownCorp", market_cap=0)]

    async def fake_sectors(client):
        return {}

    monkeypatch.setattr(smod, "fetch_market_stocks", fake_market)
    monkeypatch.setattr(smod, "build_naver_sector_mapping", fake_sectors)

    async with httpx.AsyncClient() as client:
        result = await smod.seed_stock_masters(pool, client, market="KOSDAQ")

    assert result["inserted"] == 1
    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_masters" in c[0]]
    args = inserts[0][1]
    assert args[3] is None  # market_cap=0 → None
    assert args[4] is None  # sector 없음
    assert args[5] == smod.DEFAULT_SECTOR_GROUP


@pytest.mark.asyncio
async def test_seed_stock_masters_rejects_invalid_market(monkeypatch):
    pool = _FakePool(existing=[])
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await smod.seed_stock_masters(pool, client, market="NYSE")


@pytest.mark.asyncio
async def test_seed_stock_masters_returns_zero_when_market_empty(monkeypatch):
    pool = _FakePool(existing=[])

    async def fake_market(client, market):
        return []

    async def fake_sectors(client):
        return {}

    monkeypatch.setattr(smod, "fetch_market_stocks", fake_market)
    monkeypatch.setattr(smod, "build_naver_sector_mapping", fake_sectors)

    async with httpx.AsyncClient() as client:
        result = await smod.seed_stock_masters(pool, client, market="KOSPI")

    assert result == {"inserted": 0, "updated": 0, "total": 0, "failed": 0}
    assert pool.conn.execute_calls == []


@pytest.mark.asyncio
async def test_seed_stock_masters_default_security_type_is_stock(monkeypatch):
    """kis_gateway_url 미주입 시 모든 종목 security_type='STOCK' default."""
    pool = _FakePool(existing=[])

    async def fake_market(client, market):
        return [MarketStock(stock_code="005930", stock_name="삼성전자", market_cap=400_000_000)]

    async def fake_sectors(client):
        return {"005930": "전기전자"}

    monkeypatch.setattr(smod, "fetch_market_stocks", fake_market)
    monkeypatch.setattr(smod, "build_naver_sector_mapping", fake_sectors)

    async with httpx.AsyncClient() as client:
        await smod.seed_stock_masters(pool, client, market="KOSPI")

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_masters" in c[0]]
    assert inserts[0][1][6] == "STOCK"


@pytest.mark.asyncio
async def test_seed_stock_masters_populates_security_type_via_gateway(monkeypatch):
    """kis_gateway_url 주입 시 search-info 응답의 scty_grp_id_cd 로 분류."""
    pool = _FakePool(existing=[])

    async def fake_market(client, market):
        return [
            MarketStock(stock_code="005930", stock_name="삼성전자", market_cap=400_000_000),
            MarketStock(stock_code="122630", stock_name="KODEX 레버리지", market_cap=98_000_000),
        ]

    async def fake_sectors(client):
        return {"005930": "전기전자"}

    monkeypatch.setattr(smod, "fetch_market_stocks", fake_market)
    monkeypatch.setattr(smod, "build_naver_sector_mapping", fake_sectors)
    monkeypatch.setattr(smod, "SECURITY_TYPE_RATE_LIMIT_DELAY_SEC", 0.0)

    grp_by_code = {"005930": "ST", "122630": "EF"}

    def handler(req: httpx.Request) -> httpx.Response:
        code = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"scty_grp_id_cd": grp_by_code.get(code, "")})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await smod.seed_stock_masters(
            pool, client, market="KOSPI", kis_gateway_url="http://kis-gateway:8080"
        )

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_masters" in c[0]]
    by_code = {c[1][0]: c[1][6] for c in inserts}
    assert by_code["005930"] == "STOCK"
    assert by_code["122630"] == "ETF"


@pytest.mark.asyncio
async def test_fetch_security_type_falls_back_to_stock_on_http_failure():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await smod._fetch_security_type(client, "http://kis-gateway:8080", "005930")
    assert result == "STOCK"


@pytest.mark.asyncio
async def test_fetch_security_type_unknown_grp_falls_back_to_stock():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scty_grp_id_cd": "ZZ"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await smod._fetch_security_type(client, "http://kis-gateway:8080", "005930")
    assert result == "STOCK"
