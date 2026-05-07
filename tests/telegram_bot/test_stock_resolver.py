"""resolve_stock — code/name 양방향 + DB 부재 fallback."""

from __future__ import annotations

from typing import Any

import pytest

from prime_jennie_runtime.telegram_bot.stock_resolver import resolve_stock


class _FakePool:
    def __init__(
        self,
        by_code: dict[str, dict[str, str]] | None = None,
        by_name: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._by_code = by_code or {}
        self._by_name = by_name or {}

    async def fetchrow(self, sql: str, arg: str) -> dict[str, str] | None:
        if "stock_code = $1" in sql:
            return self._by_code.get(arg)
        if "stock_name = $1" in sql:
            return self._by_name.get(arg)
        return None


@pytest.mark.asyncio
async def test_resolve_by_6digit_code_hits_table():
    pool = _FakePool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    assert await resolve_stock(pool, "005930") == ("005930", "삼성전자")


@pytest.mark.asyncio
async def test_resolve_by_name_hits_table():
    pool = _FakePool(by_name={"카카오": {"stock_code": "035720", "stock_name": "카카오"}})
    assert await resolve_stock(pool, "카카오") == ("035720", "카카오")


@pytest.mark.asyncio
async def test_unknown_code_falls_back_to_code_only():
    pool = _FakePool()
    assert await resolve_stock(pool, "999999") == ("999999", "999999")


@pytest.mark.asyncio
async def test_unknown_name_returns_none():
    pool = _FakePool()
    assert await resolve_stock(pool, "없는종목") is None


@pytest.mark.asyncio
async def test_pool_none_with_code_passes_through():
    """DB 미주입 시 6자리 숫자만 통과 (code 그대로)."""
    assert await resolve_stock(None, "005930") == ("005930", "005930")


@pytest.mark.asyncio
async def test_pool_none_with_name_returns_none():
    assert await resolve_stock(None, "삼성전자") is None


@pytest.mark.asyncio
async def test_empty_input_returns_none():
    assert await resolve_stock(None, "   ") is None


@pytest.mark.asyncio
async def test_db_error_with_code_falls_back():
    """fetchrow 가 raise 해도 6자리 코드는 통과."""

    class _BoomPool:
        async def fetchrow(self, *_args: Any) -> Any:
            raise RuntimeError("db down")

    assert await resolve_stock(_BoomPool(), "005930") == ("005930", "005930")  # type: ignore[arg-type]
    assert await resolve_stock(_BoomPool(), "삼성전자") is None  # type: ignore[arg-type]
