"""RuntimeMarketSnapshotFetcher 단위 — 두 소스 병합 + fail-open."""

from __future__ import annotations

import json
from datetime import date

import pytest

from prime_jennie_runtime.fast_loop.market_snapshot import (
    _MACRO_SNAPSHOT_KEY_PREFIX,
    RuntimeMarketSnapshotFetcher,
)


class _FakeRedis:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self._store: dict[str, bytes] = {
            k: v.encode() if isinstance(v, str) else v for k, v in (store or {}).items()
        }

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)


class _StubHttp:
    """KOSPI fetcher 는 monkeypatch 로 갈아끼우므로 객체는 sentinel."""


@pytest.mark.asyncio
async def test_all_sources_present(monkeypatch):
    today_key = f"{_MACRO_SNAPSHOT_KEY_PREFIX}{date.today().isoformat()}"
    redis = _FakeRedis({today_key: json.dumps({"vix": 22.5, "sox_change_pct": -1.2})})

    async def fake_fetch_kospi(_client):
        return -1.5

    monkeypatch.setattr(
        "prime_jennie_runtime.fast_loop.market_snapshot.fetch_kospi_change_pct", fake_fetch_kospi
    )
    fetcher = RuntimeMarketSnapshotFetcher(
        http=_StubHttp(),  # type: ignore[arg-type]
        redis_client=redis,  # type: ignore[arg-type]
    )

    kospi, vix, sox = await fetcher()
    assert kospi == pytest.approx(-1.5)
    assert vix == pytest.approx(22.5)
    assert sox == pytest.approx(-1.2)


@pytest.mark.asyncio
async def test_kospi_fetch_returns_none_falls_back_to_zero(monkeypatch):
    redis = _FakeRedis({})

    async def fake_fetch_kospi(_client):
        return None

    monkeypatch.setattr(
        "prime_jennie_runtime.fast_loop.market_snapshot.fetch_kospi_change_pct", fake_fetch_kospi
    )
    fetcher = RuntimeMarketSnapshotFetcher(
        http=_StubHttp(),  # type: ignore[arg-type]
        redis_client=redis,  # type: ignore[arg-type]
    )

    kospi, vix, sox = await fetcher()
    assert kospi == 0.0
    assert vix is None
    assert sox is None


@pytest.mark.asyncio
async def test_redis_corrupt_payload_returns_none(monkeypatch):
    today_key = f"{_MACRO_SNAPSHOT_KEY_PREFIX}{date.today().isoformat()}"
    redis = _FakeRedis({today_key: "not-json"})

    async def fake_fetch_kospi(_client):
        return 0.0

    monkeypatch.setattr(
        "prime_jennie_runtime.fast_loop.market_snapshot.fetch_kospi_change_pct", fake_fetch_kospi
    )
    fetcher = RuntimeMarketSnapshotFetcher(
        http=_StubHttp(),  # type: ignore[arg-type]
        redis_client=redis,  # type: ignore[arg-type]
    )

    _, vix, sox = await fetcher()
    assert vix is None
    assert sox is None


@pytest.mark.asyncio
async def test_fetch_kospi_change_pct_handles_http_error():
    """fetch_kospi_change_pct 자체가 raise 안 함 — 통신 오류 시 None."""
    import httpx

    from prime_jennie_runtime.fast_loop.market_snapshot import fetch_kospi_change_pct

    transport = httpx.MockTransport(lambda _r: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_kospi_change_pct(client)
    assert result is None
