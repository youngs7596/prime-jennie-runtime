"""PriceRepo — v3 daily_prices / minute_prices read-through 캐시.

- KIS API 응답을 저장해 circuit open / rate limit 시 fallback.
- InMemoryPriceRepo: 단위 테스트 + dev 환경.
- PostgresPriceRepo: 운영. asyncpg 기반 UPSERT + LIMIT 조회.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from .schemas import DailyPrice, MinutePrice


@runtime_checkable
class PriceRepo(Protocol):
    """시세 read-through 캐시."""

    async def fetch_daily(self, stock_code: str, days: int) -> list[DailyPrice]:
        """최근 `days` 일치. 최근순(날짜 desc) → KIS API 응답과 동일 정렬."""
        ...

    async def fetch_minute(self, stock_code: str) -> list[MinutePrice]:
        """가장 최근 분봉 1세트 (KIS 는 통상 ~120개)."""
        ...

    async def upsert_daily(self, items: list[DailyPrice]) -> int:
        """저장 성공 row 수 반환 (중복 upsert 포함)."""
        ...

    async def upsert_minute(self, items: list[MinutePrice]) -> int: ...


# ---------------------------------------------------------------------
# InMemoryPriceRepo (dev/tests)
# ---------------------------------------------------------------------


@dataclass
class InMemoryPriceRepo:
    """단일 프로세스 내 in-memory 저장소."""

    _daily: dict[tuple[str, date], DailyPrice] = field(default_factory=dict)
    _minute: dict[tuple[str, datetime], MinutePrice] = field(default_factory=dict)

    async def fetch_daily(self, stock_code: str, days: int) -> list[DailyPrice]:
        rows = [v for (code, _d), v in self._daily.items() if code == stock_code]
        rows.sort(key=lambda x: x.price_date, reverse=True)
        return rows[:days]

    async def fetch_minute(self, stock_code: str) -> list[MinutePrice]:
        rows = [v for (code, _dt), v in self._minute.items() if code == stock_code]
        rows.sort(key=lambda x: x.price_datetime, reverse=True)
        return rows

    async def upsert_daily(self, items: list[DailyPrice]) -> int:
        for it in items:
            self._daily[(it.stock_code, it.price_date)] = it
        return len(items)

    async def upsert_minute(self, items: list[MinutePrice]) -> int:
        for it in items:
            self._minute[(it.stock_code, it.price_datetime)] = it
        return len(items)


# ---------------------------------------------------------------------
# PostgresPriceRepo (asyncpg)
# ---------------------------------------------------------------------


_DAILY_COLUMNS = (
    "stock_code",
    "price_date",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "change_pct",
)
_MINUTE_COLUMNS = (
    "stock_code",
    "price_datetime",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
)

_DAILY_PLACEHOLDERS = ", ".join(f"${i + 1}" for i in range(len(_DAILY_COLUMNS)))
_MINUTE_PLACEHOLDERS = ", ".join(f"${i + 1}" for i in range(len(_MINUTE_COLUMNS)))

_DAILY_UPSERT = (
    f"INSERT INTO daily_prices ({', '.join(_DAILY_COLUMNS)}) "
    f"VALUES ({_DAILY_PLACEHOLDERS}) "
    "ON CONFLICT (stock_code, price_date) DO UPDATE SET "
    "open_price=EXCLUDED.open_price, high_price=EXCLUDED.high_price, "
    "low_price=EXCLUDED.low_price, close_price=EXCLUDED.close_price, "
    "volume=EXCLUDED.volume, change_pct=EXCLUDED.change_pct, updated_at=NOW()"
)
_MINUTE_UPSERT = (
    f"INSERT INTO minute_prices ({', '.join(_MINUTE_COLUMNS)}) "
    f"VALUES ({_MINUTE_PLACEHOLDERS}) "
    "ON CONFLICT (stock_code, price_datetime) DO UPDATE SET "
    "open_price=EXCLUDED.open_price, high_price=EXCLUDED.high_price, "
    "low_price=EXCLUDED.low_price, close_price=EXCLUDED.close_price, "
    "volume=EXCLUDED.volume, updated_at=NOW()"
)

_DAILY_SELECT = (
    f"SELECT {', '.join(_DAILY_COLUMNS)} FROM daily_prices "
    "WHERE stock_code = $1 ORDER BY price_date DESC LIMIT $2"
)
_MINUTE_SELECT = (
    f"SELECT {', '.join(_MINUTE_COLUMNS)} FROM minute_prices "
    "WHERE stock_code = $1 ORDER BY price_datetime DESC"
)


@dataclass
class PostgresPriceRepo:
    """asyncpg 기반. ``conn`` 은 단일 Connection 또는 Pool 모두 허용
    (둘 다 ``fetch``/``execute``/``executemany`` 인터페이스 호환)."""

    conn: Any  # asyncpg.Connection | asyncpg.Pool

    async def fetch_daily(self, stock_code: str, days: int) -> list[DailyPrice]:
        records = await self.conn.fetch(_DAILY_SELECT, stock_code, days)
        return [DailyPrice(**dict(r)) for r in records]

    async def fetch_minute(self, stock_code: str) -> list[MinutePrice]:
        records = await self.conn.fetch(_MINUTE_SELECT, stock_code)
        return [MinutePrice(**dict(r)) for r in records]

    async def upsert_daily(self, items: list[DailyPrice]) -> int:
        if not items:
            return 0
        tuples = [tuple(getattr(it, c) for c in _DAILY_COLUMNS) for it in items]
        await self.conn.executemany(_DAILY_UPSERT, tuples)
        return len(tuples)

    async def upsert_minute(self, items: list[MinutePrice]) -> int:
        if not items:
            return 0
        tuples = [tuple(getattr(it, c) for c in _MINUTE_COLUMNS) for it in items]
        await self.conn.executemany(_MINUTE_UPSERT, tuples)
        return len(tuples)


__all__ = ["InMemoryPriceRepo", "PostgresPriceRepo", "PriceRepo"]
