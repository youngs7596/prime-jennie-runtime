"""v3 Postgres → 브리핑 데이터 dict 어댑터.

v2 repo 를 직접 포팅할 수 없는 부분(Position/Watchlist/Asset 등)은 v3 테이블 기반
대체로 채운다. 테이블이 없거나 비어있으면 None/[] 로 통일 (fallback HTML 이 섹션 자체를 생략).

포팅 원본: prime-jennie/prime_jennie/services/briefing/reporter.py `collect_report_data`
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .formatters import compute_trade_summary

logger = logging.getLogger(__name__)


def _parse_json_field(raw: str | dict | list | None) -> list | dict | None:
    """JSON 문자열/이미 파싱된 dict-list 모두 허용."""
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def collect_briefing_data(
    engine: AsyncEngine,
    *,
    as_of: date | None = None,
) -> dict:
    """v3 DB 를 읽어 브리핑 데이터 dict 를 만든다.

    Returns:
        formatters.build_llm_context / format_fallback_html 이 소비하는 dict.
    """
    today = as_of or date.today()

    async with engine.connect() as conn:
        trades = await _collect_trades(conn, today)
        positions = await _collect_positions(conn)
        macro = await _collect_macro(conn)
        watchlist = await _collect_watchlist(conn)
        news = await _collect_news(conn, today)

    return {
        "date": today.isoformat(),
        "positions": positions,
        "trades": trades,
        "trade_summary": compute_trade_summary(trades),
        "macro": macro,
        "watchlist": watchlist,
        "assets": None,  # v3 에 asset_snapshots 없음 — Track B KIS sync 이후 채움
        "news": news,
    }


# =====================================================================
# 각 섹션
# =====================================================================


async def _collect_trades(conn, today: date) -> list[dict]:
    """오늘 체결된 매매 내역.

    v3 에선 `executions` + `position_sheets` + `outcomes` 조합으로 구성.
    executions 한 행 = 매매 한 건. 매도 건은 outcomes.pnl_* 로 profit_* 채움.
    """
    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    stmt = text(
        """
        SELECT
            e.side,
            e.price,
            e.qty,
            e.executed_at,
            ps.ticker AS stock_code,
            ps.strategy_tag,
            o.pnl_krw,
            o.pnl_pct,
            o.exit_reason
        FROM executions e
        JOIN position_sheets ps ON ps.sheet_id = e.sheet_id
        LEFT JOIN outcomes o ON o.sheet_id = e.sheet_id
        WHERE e.executed_at >= :day_start AND e.executed_at < :day_end
        ORDER BY e.executed_at
        """
    )
    rows = (await conn.execute(stmt, {"day_start": day_start, "day_end": day_end})).mappings().all()

    trades: list[dict] = []
    for r in rows:
        side = str(r["side"]).upper()
        is_sell = side in ("SELL", "S")
        reason = r["exit_reason"] if is_sell and r["exit_reason"] else r["strategy_tag"]
        trades.append(
            {
                "stock_code": r["stock_code"],
                "stock_name": r["stock_code"],  # v3 에 종목명 캐시 없음 — code 로 대체
                "trade_type": "SELL" if is_sell else "BUY",
                "quantity": int(r["qty"]),
                "price": float(r["price"]),
                "total_amount": float(r["price"]) * int(r["qty"]),
                "reason": reason or "",
                "profit_pct": float(r["pnl_pct"]) if is_sell and r["pnl_pct"] is not None else None,
                "profit_amount": (
                    float(r["pnl_krw"]) if is_sell and r["pnl_krw"] is not None else None
                ),
            }
        )
    return trades


async def _collect_positions(conn) -> list[dict]:
    """미청산 포지션 (outcomes 미기록 시트 → 아직 보유 중).

    주의: v3 는 KIS snapshot 연동이 아직 없다. 시트 기반 근사치.
    Track B sync-positions 이후 이 함수는 v3 portfolio 테이블을 읽도록 교체 예정.
    """
    stmt = text(
        """
        SELECT
            ps.ticker AS stock_code,
            SUM(CASE WHEN e.side IN ('buy','BUY','B') THEN e.qty ELSE -e.qty END) AS quantity,
            AVG(CASE WHEN e.side IN ('buy','BUY','B') THEN e.price END) AS avg_price
        FROM position_sheets ps
        JOIN executions e ON e.sheet_id = ps.sheet_id
        LEFT JOIN outcomes o ON o.sheet_id = ps.sheet_id
        WHERE o.closed_at IS NULL
        GROUP BY ps.ticker
        HAVING SUM(CASE WHEN e.side IN ('buy','BUY','B') THEN e.qty ELSE -e.qty END) > 0
        ORDER BY ps.ticker
        """
    )
    rows = (await conn.execute(stmt)).mappings().all()
    positions: list[dict] = []
    for r in rows:
        qty = int(r["quantity"] or 0)
        avg_price = float(r["avg_price"] or 0)
        positions.append(
            {
                "stock_code": r["stock_code"],
                "stock_name": r["stock_code"],
                "quantity": qty,
                "avg_price": avg_price,
                "total_buy": qty * avg_price,
            }
        )
    return positions


async def _collect_macro(conn) -> dict | None:
    """최신 macro_runs → v2 MacroInsight 스키마 호환 dict.

    v2 가 기대하는 확장 필드(kospi_index, vix, council_consensus 등)는 v3 에 없음.
    현재는 gate/size_multiplier + reasoning 만 채운다.
    """
    stmt = text(
        """
        SELECT gate, size_multiplier, reasoning, top_risks_json,
               confidence, next_review_hint, generated_at
        FROM macro_runs
        ORDER BY generated_at DESC
        LIMIT 1
        """
    )
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        return None

    risks = _parse_json_field(row["top_risks_json"])
    risk_descriptions: list[str] = []
    if isinstance(risks, list):
        for r in risks:
            if isinstance(r, dict) and r.get("description"):
                risk_descriptions.append(str(r["description"]))
            elif isinstance(r, str):
                risk_descriptions.append(r)

    return {
        "sentiment": row["gate"],  # open/closed 를 심리로 노출
        "sentiment_score": float(row["size_multiplier"]) if row["size_multiplier"] else None,
        "regime_hint": row["confidence"],
        "kospi_index": None,
        "kospi_change_pct": None,
        "kosdaq_index": None,
        "kosdaq_change_pct": None,
        "vix_value": None,
        "vix_regime": None,
        "usd_krw": None,
        "council_consensus": None,
        "risk_factors": risk_descriptions or None,
        "key_themes": None,
        "trading_reasoning": row["reasoning"],
        "sectors_to_favor": None,
        "sectors_to_avoid": None,
        "next_review_hint": row["next_review_hint"],
    }


async def _collect_watchlist(conn) -> list[dict]:
    """최신 legacy_quant_scores 에서 is_final_selected Top 10."""
    stmt = text(
        """
        SELECT stock_code, stock_name, hybrid_score, trade_tier
        FROM legacy_quant_scores
        WHERE score_date = (
            SELECT MAX(score_date) FROM legacy_quant_scores
            WHERE is_final_selected = TRUE AND is_active = TRUE
        )
        AND is_final_selected = TRUE
        AND is_active = TRUE
        ORDER BY hybrid_score DESC NULLS LAST
        LIMIT 10
        """
    )
    try:
        rows = (await conn.execute(stmt)).mappings().all()
    except Exception:
        logger.warning("legacy_quant_scores 조회 실패 — 빈 워치리스트 반환", exc_info=True)
        return []
    return [
        {
            "stock_code": r["stock_code"],
            "stock_name": r["stock_name"],
            "hybrid_score": float(r["hybrid_score"]) if r["hybrid_score"] is not None else None,
            "trade_tier": r["trade_tier"],
            "rank": idx + 1,
        }
        for idx, r in enumerate(rows)
    ]


async def _collect_news(conn, today: date) -> list[dict]:
    """최근 24시간 news_sentiments 상위 5건."""
    since = datetime.combine(today, datetime.min.time()) - timedelta(days=1)
    stmt = text(
        """
        SELECT ns.ticker, ns.score, na.title
        FROM news_sentiments ns
        LEFT JOIN news_articles na ON na.article_id = ns.article_id
        WHERE ns.analyzed_at >= :since
        ORDER BY ABS(ns.score) DESC
        LIMIT 5
        """
    )
    try:
        rows = (await conn.execute(stmt, {"since": since})).mappings().all()
    except Exception:
        logger.warning("news_sentiments 조회 실패 — 빈 뉴스 반환", exc_info=True)
        return []
    return [
        {
            "stock_code": r["ticker"],
            "headline": (r["title"] or "")[:80],
            "score": float(r["score"]),
        }
        for r in rows
    ]


__all__ = ["collect_briefing_data"]
