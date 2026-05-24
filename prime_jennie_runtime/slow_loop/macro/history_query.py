"""Macro 직전 이력 조회 — prompt 의 `recent_runs` 컨텍스트와 continuity 체크용.

`run_slow_loop` 호출자 (slow_loop/app.py 의 scout_daily, trigger_watcher 의
make_invoker) 가 매 실행 직전에 호출해 `RecentMacroRun` list 를 만들어 넘긴다.
없이 넘기면 prompt 의 직전 이력 부분이 "(이력 없음)" 이 되고 abrupt_transition
체크도 첫 분기에서 즉시 False 가 된다 (continuity.py:16).

DB 조회 실패는 fail-open (빈 list 반환) — slow_loop 자체를 막진 않는다.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .schemas import RecentMacroRun

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 7


async def fetch_recent_macro_runs(
    engine: AsyncEngine, *, limit: int = DEFAULT_LIMIT
) -> list[RecentMacroRun]:
    """최신순 N건의 macro_run 을 RecentMacroRun list 로 반환.

    `top_risks_summary` 는 PG 의 `top_risks_json` (RiskItem list) 에서
    description 들을 `"; "` 로 join — pipeline.py 의 영속화 직전 변환과 동일.
    파싱 실패한 row 는 빈 summary 로 fallback 하고 list 에는 포함.
    """
    sql = text(
        """
        SELECT macro_run_id, generated_at, gate, size_multiplier, top_risks_json
        FROM macro_runs
        ORDER BY generated_at DESC
        LIMIT :limit
        """
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(sql, {"limit": int(limit)})
            rows = result.mappings().all()
    except Exception:
        logger.exception("fetch_recent_macro_runs failed — returning empty list")
        return []

    out: list[RecentMacroRun] = []
    for row in rows:
        out.append(
            RecentMacroRun(
                macro_run_id=row["macro_run_id"],
                generated_at=row["generated_at"],
                gate=row["gate"],
                size_multiplier=float(row["size_multiplier"]),
                top_risks_summary=_summarize_top_risks(row["top_risks_json"]),
            )
        )
    return out


def _summarize_top_risks(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            return ""
    else:
        items = raw
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            desc = item.get("description")
            if isinstance(desc, str) and desc:
                parts.append(desc)
    return "; ".join(parts)


__all__ = ["DEFAULT_LIMIT", "fetch_recent_macro_runs"]
