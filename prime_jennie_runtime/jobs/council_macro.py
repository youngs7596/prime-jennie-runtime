"""Macro / Council 관련 job — 외부 지표 수집, 검증, council trigger.

v2 원본:
- `/jobs/macro-collect-global` (app.py:1016-1109)
- `/jobs/macro-collect-korea` (app.py:1112-1119) — global 위임
- `/jobs/macro-validate-store` (app.py:1122-1171)
- `/jobs/macro-quick` (app.py:1174-...) — global + intraday risk
- `/jobs/council-trigger` (app.py:1688-...)
- `/jobs/council-insight` (app.py:1791-...)

Redis key 스키마는 v2 와 호환 유지 (macro:data:snapshot:{YYYY-MM-DD}). council
핸들러는 Track D `council_logging` 과 조율 중 — 여기선 trigger/insight 껍데기만.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

MACRO_SNAPSHOT_KEY_PREFIX = "macro:data:snapshot:"
MACRO_VALIDATE_LOOKBACK_DAYS = 7
MACRO_REQUIRED_FIELDS: tuple[str, ...] = ("kospi_index", "kosdaq_index")
MACRO_KOSPI_RANGE = (1000, 10000)
MACRO_KOSDAQ_RANGE = (300, 3000)


class MacroValidationError(RuntimeError):
    """macro_validate_store 가 스냅샷을 찾지 못하거나 필수 필드가 빠졌을 때."""


async def macro_validate_store(redis_client: Any) -> dict[str, Any]:
    """v2 `/jobs/macro-validate-store` 포팅 (app.py:1122-1171).

    최근 7 일 이내의 Redis 스냅샷 (`macro:data:snapshot:{YYYY-MM-DD}`) 을 찾아
    필수 필드 존재 + 값 범위 sanity check. v2 가 `JobResult(success=False,...)` 로
    돌려주던 상황은 v3 에선 `MacroValidationError` 로 올린다 (scheduler runner
    가 `last_status=failed` 에 메시지 기록).
    """
    snapshot_data: dict[str, Any] | None = None
    found_key: str | None = None
    today = date.today()
    for days_ago in range(MACRO_VALIDATE_LOOKBACK_DAYS):
        check_date = today - timedelta(days=days_ago)
        key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{check_date.isoformat()}"
        raw = await redis_client.get(key)
        if raw is not None:
            snapshot_data = json.loads(raw)
            found_key = key
            break

    if snapshot_data is None:
        raise MacroValidationError(
            f"No macro snapshot found in last {MACRO_VALIDATE_LOOKBACK_DAYS} days"
        )

    missing = [
        f for f in MACRO_REQUIRED_FIELDS if f not in snapshot_data or snapshot_data[f] is None
    ]
    if missing:
        raise MacroValidationError(
            f"Macro validation failed — missing fields: {', '.join(missing)}"
        )

    warnings: list[str] = []
    kospi = snapshot_data.get("kospi_index", 0)
    if kospi < MACRO_KOSPI_RANGE[0] or kospi > MACRO_KOSPI_RANGE[1]:
        warnings.append(f"KOSPI index unusual: {kospi}")
    kosdaq = snapshot_data.get("kosdaq_index", 0)
    if kosdaq < MACRO_KOSDAQ_RANGE[0] or kosdaq > MACRO_KOSDAQ_RANGE[1]:
        warnings.append(f"KOSDAQ index unusual: {kosdaq}")

    fields_present = len(
        [k for k, v in snapshot_data.items() if v is not None and k != "data_sources"]
    )
    summary = {
        "key": found_key,
        "snapshot_date": snapshot_data.get("snapshot_date"),
        "fields_present": fields_present,
        "warnings": warnings,
    }
    logger.info(
        "macro_validate_store: key=%s fields=%d warnings=%d",
        found_key,
        fields_present,
        len(warnings),
    )
    return summary


__all__ = [
    "MACRO_KOSDAQ_RANGE",
    "MACRO_KOSPI_RANGE",
    "MACRO_REQUIRED_FIELDS",
    "MACRO_SNAPSHOT_KEY_PREFIX",
    "MACRO_VALIDATE_LOOKBACK_DAYS",
    "MacroValidationError",
    "macro_validate_store",
]
