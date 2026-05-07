"""종목 코드 / 이름 → (code, name) 해석.

텔레그램 명령에서 사용자가 "삼성전자" 또는 "005930" 으로 입력해도 동일하게
처리하기 위한 해석기. v2 ``handler._resolve_stock`` async 포팅.

순서:
1. 6자리 숫자면 ``stock_code`` 로 직접 lookup. 미존재 시 (code, code) fallback.
2. 그 외엔 ``stock_name`` 정확히 일치하는 행 lookup.
3. 미발견이면 None.

DB 미주입 (pool=None) 시: 6자리 숫자만 통과 (code 그대로 반환), 이름 입력은
None. 실패-친화적이라 telegram-bot 컨테이너 첫 기동 마이그레이션 전에도
/price 005930 같은 코드 입력은 동작.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def resolve_stock(pool: Any | None, name_or_code: str) -> tuple[str, str] | None:
    """``(stock_code, stock_name)`` 또는 None."""
    name_or_code = name_or_code.strip()
    if not name_or_code:
        return None

    is_code = name_or_code.isdigit() and len(name_or_code) == 6

    if pool is None:
        return (name_or_code, name_or_code) if is_code else None

    try:
        if is_code:
            row = await pool.fetchrow(
                "SELECT stock_code, stock_name FROM stock_masters WHERE stock_code = $1",
                name_or_code,
            )
            if row is not None:
                return row["stock_code"], row["stock_name"]
            return name_or_code, name_or_code

        row = await pool.fetchrow(
            "SELECT stock_code, stock_name FROM stock_masters WHERE stock_name = $1",
            name_or_code,
        )
        if row is not None:
            return row["stock_code"], row["stock_name"]
        return None
    except Exception:
        logger.exception("stock_masters lookup failed for %s", name_or_code)
        # DB 장애 시에도 6자리 코드는 통과
        if is_code:
            return name_or_code, name_or_code
        return None


__all__ = ["resolve_stock"]
