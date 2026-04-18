"""scripts/backfill_daily_prices.py — 단위 테스트.

실 DB / KIS Gateway 미호출. argparse / DSN 조립 / URL 해석 / rate limit 간격 /
retry 로직 검증.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from scripts.backfill_daily_prices import (
    _call_with_retry,
    _parse_args,
    dsn_from_env,
    resolve_gateway_url,
)

# ─── argparse ─────────────────────────────────────────────────────


def test_parse_args_defaults():
    args = _parse_args([])
    assert args.limit == 200
    assert args.days == 60
    assert args.sleep == 0.5  # paper 2/sec 가정
    assert args.gateway_url is None
    assert args.max_attempts == 3
    assert args.backoff == 1.0
    assert args.dry_run is False


def test_parse_args_overrides():
    args = _parse_args(
        [
            "--limit",
            "50",
            "--days",
            "30",
            "--sleep",
            "0.06",
            "--gateway-url",
            "http://localhost:8080",
            "--max-attempts",
            "5",
            "--backoff",
            "0.5",
            "--dry-run",
        ]
    )
    assert args.limit == 50
    assert args.days == 30
    assert args.sleep == 0.06
    assert args.gateway_url == "http://localhost:8080"
    assert args.max_attempts == 5
    assert args.backoff == 0.5
    assert args.dry_run is True


# ─── DSN ──────────────────────────────────────────────────────────


def test_dsn_uses_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "alice")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "testdb")
    assert dsn_from_env() == "postgres://alice:secret@db.internal:5433/testdb"


def test_dsn_defaults(monkeypatch):
    for k in (
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(k, raising=False)
    # pj_runtime 이 backfill 의 기본 계정. INSERT 권한 있음 (003 migration).
    assert dsn_from_env() == "postgres://pj_runtime:dev_password@localhost:5432/prime_jennie_v3"


# ─── Gateway URL ──────────────────────────────────────────────────


def test_gateway_url_cli_wins(monkeypatch):
    monkeypatch.setenv("KIS_GATEWAY_URL", "http://env-host:9000")
    assert resolve_gateway_url("http://cli-host:8000/") == "http://cli-host:8000"


def test_gateway_url_env_fallback(monkeypatch):
    monkeypatch.setenv("KIS_GATEWAY_URL", "http://env-host:9000/")
    assert resolve_gateway_url(None) == "http://env-host:9000"


def test_gateway_url_docker_default(monkeypatch):
    monkeypatch.delenv("KIS_GATEWAY_URL", raising=False)
    # docker compose 내부 DNS. 로컬 WSL 에선 CLI --gateway-url 로 덮어써야 함.
    assert resolve_gateway_url(None) == "http://kis-gateway:8080"


# ─── retry 로직 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_with_retry_succeeds_first_try():
    """첫 호출 성공 시 재시도 없음."""
    fake = AsyncMock(return_value=60)
    with patch("scripts.backfill_daily_prices._fetch_and_upsert_daily", fake):
        result = await _call_with_retry(
            http=None,  # type: ignore[arg-type]
            repo=None,  # type: ignore[arg-type]
            gateway_url="http://g",
            ticker="005930",
            days=60,
            max_attempts=3,
            backoff=0.01,
        )
    assert result == 60
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_call_with_retry_retries_then_succeeds():
    """transient 실패 → 재시도 후 성공."""
    calls = {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return 42

    with patch("scripts.backfill_daily_prices._fetch_and_upsert_daily", flaky):
        result = await _call_with_retry(
            http=None,  # type: ignore[arg-type]
            repo=None,  # type: ignore[arg-type]
            gateway_url="http://g",
            ticker="005930",
            days=60,
            max_attempts=5,
            backoff=0.001,
        )
    assert result == 42
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_call_with_retry_raises_after_max_attempts():
    """재시도 한계 초과 시 마지막 예외 전파."""
    fake = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("scripts.backfill_daily_prices._fetch_and_upsert_daily", fake),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await _call_with_retry(
            http=None,  # type: ignore[arg-type]
            repo=None,  # type: ignore[arg-type]
            gateway_url="http://g",
            ticker="005930",
            days=60,
            max_attempts=2,
            backoff=0.001,
        )
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_call_with_retry_uses_exponential_backoff():
    """backoff 는 1 → 2 → 4 로 증가해야 함."""
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    fake = AsyncMock(side_effect=RuntimeError("nope"))
    with (
        patch("scripts.backfill_daily_prices._fetch_and_upsert_daily", fake),
        patch("scripts.backfill_daily_prices.asyncio.sleep", fake_sleep),
        pytest.raises(RuntimeError),
    ):
        await _call_with_retry(
            http=None,  # type: ignore[arg-type]
            repo=None,  # type: ignore[arg-type]
            gateway_url="http://g",
            ticker="005930",
            days=60,
            max_attempts=4,
            backoff=0.5,
        )
    # 3 번의 sleep (attempt 1,2,3 의 실패 후). attempt 4 는 raise 직행.
    assert slept == [0.5, 1.0, 2.0]


# ─── backfill() universe empty ────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_returns_2_on_empty_universe():
    """market_cap NOT NULL row 가 없으면 exit code 2."""
    from scripts.backfill_daily_prices import backfill

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.close = AsyncMock()

    async def fake_create_pool(*args: Any, **kwargs: Any) -> Any:
        return fake_pool

    with patch("scripts.backfill_daily_prices.asyncpg.create_pool", fake_create_pool):
        code = await backfill(
            dsn="postgres://x",
            gateway_url="http://g",
            limit=10,
            days=60,
            sleep_sec=0.0,
            max_attempts=1,
            backoff=0.0,
            dry_run=False,
        )
    assert code == 2
    fake_pool.close.assert_awaited()


@pytest.mark.asyncio
async def test_backfill_dry_run_skips_http():
    """--dry-run 은 httpx 호출 없이 universe 만 로깅하고 0 반환."""
    from scripts.backfill_daily_prices import backfill

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[{"stock_code": "005930"}, {"stock_code": "000660"}])
    fake_pool.close = AsyncMock()

    async def fake_create_pool(*args: Any, **kwargs: Any) -> Any:
        return fake_pool

    fetch_spy = AsyncMock()
    with (
        patch("scripts.backfill_daily_prices.asyncpg.create_pool", fake_create_pool),
        patch("scripts.backfill_daily_prices._fetch_and_upsert_daily", fetch_spy),
    ):
        code = await backfill(
            dsn="postgres://x",
            gateway_url="http://g",
            limit=10,
            days=60,
            sleep_sec=0.0,
            max_attempts=1,
            backoff=0.0,
            dry_run=True,
        )
    assert code == 0
    fetch_spy.assert_not_awaited()


# ─── rate limit 간격 검증 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_sleeps_between_tickers():
    """sleep_sec > 0 일 때 종목간 asyncio.sleep 호출."""
    from scripts.backfill_daily_prices import backfill

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(
        return_value=[
            {"stock_code": "005930"},
            {"stock_code": "000660"},
            {"stock_code": "035720"},
        ]
    )
    fake_pool.close = AsyncMock()

    async def fake_create_pool(*args: Any, **kwargs: Any) -> Any:
        return fake_pool

    slept: list[float] = []
    original_sleep = asyncio.sleep

    async def record_sleep(s: float) -> None:
        slept.append(s)
        await original_sleep(0)

    with (
        patch("scripts.backfill_daily_prices.asyncpg.create_pool", fake_create_pool),
        patch(
            "scripts.backfill_daily_prices._fetch_and_upsert_daily",
            AsyncMock(return_value=1),
        ),
        patch("scripts.backfill_daily_prices.asyncio.sleep", record_sleep),
    ):
        code = await backfill(
            dsn="postgres://x",
            gateway_url="http://g",
            limit=3,
            days=60,
            sleep_sec=0.5,
            max_attempts=1,
            backoff=0.0,
            dry_run=False,
        )
    assert code == 0
    # 3 종목 → 2 번의 inter-ticker sleep.
    assert slept == [0.5, 0.5]


@pytest.mark.asyncio
async def test_backfill_returns_1_when_some_fail():
    """일부 실패 시 exit code 1."""
    from scripts.backfill_daily_prices import backfill

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[{"stock_code": "005930"}, {"stock_code": "000660"}])
    fake_pool.close = AsyncMock()

    async def fake_create_pool(*args: Any, **kwargs: Any) -> Any:
        return fake_pool

    calls = {"n": 0}

    async def half_fail(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("KIS 502")
        return 60

    with (
        patch("scripts.backfill_daily_prices.asyncpg.create_pool", fake_create_pool),
        patch("scripts.backfill_daily_prices._fetch_and_upsert_daily", half_fail),
    ):
        code = await backfill(
            dsn="postgres://x",
            gateway_url="http://g",
            limit=2,
            days=60,
            sleep_sec=0.0,
            max_attempts=1,
            backoff=0.0,
            dry_run=False,
        )
    assert code == 1
