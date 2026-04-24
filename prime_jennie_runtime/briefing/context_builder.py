"""v3 Postgres + Redis → 브리핑 데이터 dict 어댑터.

v2 repo 를 직접 포팅할 수 없는 부분(Position/Watchlist/Asset 등)은 v3 테이블 기반
대체로 채운다. 테이블이 없거나 비어있으면 None/[] 로 통일 (fallback HTML 이 섹션 자체를 생략).

매크로 지표(VIX/닛케이/항셍/SOX/NVDA/원자재/환율/수급)는 Redis `macro:data:snapshot:{YYYY-MM-DD}`
에서 읽는다. `macro_collect_global` cron 이 하루 2회 (07:40/22:40 KST) 갱신, TTL 7일.
redis_client 미주입 시 macro dict 엔 KOSPI/KOSDAQ + macro_runs 값만 채워진다.

포팅 원본: prime-jennie/prime_jennie/services/briefing/reporter.py `collect_report_data`
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .formatters import compute_trade_summary

logger = logging.getLogger(__name__)

MACRO_SNAPSHOT_KEY_PREFIX = "macro:data:snapshot:"


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
    redis_client: Any | None = None,
) -> dict:
    """v3 DB + Redis 를 읽어 브리핑 데이터 dict 를 만든다.

    redis_client 는 optional — 없으면 매크로 dict 에 VIX/닛케이/항셍/SOX/NVDA/환율/수급
    필드가 채워지지 않는다.

    Returns:
        formatters.build_llm_context / format_fallback_html 이 소비하는 dict.
    """
    today = as_of or date.today()

    async with engine.connect() as conn:
        trades = await _collect_trades(conn, today)
        positions = await _collect_positions(conn)
        macro = await _collect_macro(conn, today, redis_client=redis_client)
        watchlist = await _collect_watchlist(conn)
        news = await _collect_news(conn, today)
        assets = await _collect_assets(conn, today)

    return {
        "date": today.isoformat(),
        "positions": positions,
        "trades": trades,
        "trade_summary": compute_trade_summary(trades),
        "macro": macro,
        "watchlist": watchlist,
        "assets": assets,
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


async def _collect_macro(conn, today: date, *, redis_client: Any | None = None) -> dict | None:
    """최신 macro_runs + KOSPI/KOSDAQ (index_daily_prices) + Redis macro snapshot 병합.

    - gate/size/reasoning: macro_runs 최신
    - KOSPI/KOSDAQ index + change_pct: index_daily_prices 최근 2일
    - VIX/닛케이/항셍/SOX/NVDA/환율/원자재/수급: Redis `macro:data:snapshot:{today}`
      (redis_client 미주입 또는 key 부재 시 해당 필드 생략)
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

    indices = await _fetch_latest_indices(conn, today)
    snapshot = await _fetch_macro_snapshot(redis_client, today)

    # DB index 가 없으면 Redis snapshot 의 kospi/kosdaq 로 fallback
    kospi_index = indices.get("kospi_index") or snapshot.get("kospi_index")
    kospi_change = indices.get("kospi_change_pct")
    if kospi_change is None:
        kospi_change = snapshot.get("kospi_change_pct")
    kosdaq_index = indices.get("kosdaq_index") or snapshot.get("kosdaq_index")
    kosdaq_change = indices.get("kosdaq_change_pct")
    if kosdaq_change is None:
        kosdaq_change = snapshot.get("kosdaq_change_pct")

    macro: dict[str, Any] = {
        "sentiment": row["gate"],  # open/closed 를 심리로 노출
        "sentiment_score": float(row["size_multiplier"]) if row["size_multiplier"] else None,
        "regime_hint": row["confidence"],
        "kospi_index": kospi_index,
        "kospi_change_pct": kospi_change,
        "kosdaq_index": kosdaq_index,
        "kosdaq_change_pct": kosdaq_change,
        "risk_factors": risk_descriptions or None,
        "trading_reasoning": row["reasoning"],
        "next_review_hint": row["next_review_hint"],
    }

    # Redis snapshot 에서 가져온 글로벌 매크로/수급 필드를 병합.
    # macro_collect_global cron(07:40/22:40 KST) 이 실패했거나 필드가 비면 그냥 생략.
    for key in (
        "vix",
        "vix_regime",
        "sox_close",
        "sox_change_pct",
        "nvda_close",
        "nvda_change_pct",
        "nikkei_close",
        "nikkei_change_pct",
        "hsi_close",
        "hsi_change_pct",
        "usd_jpy",
        "usd_jpy_change_pct",
        "usd_krw",  # BOK_ECOS_API_KEY 있을 때만 채워짐
        "crude_oil",
        "crude_oil_change_pct",
        "gold",
        "gold_change_pct",
        "kospi_foreign_net",
        "kospi_institutional_net",
        "kospi_retail_net",
    ):
        if key in snapshot and snapshot[key] is not None:
            macro[key] = snapshot[key]

    return macro


async def _fetch_macro_snapshot(redis_client: Any | None, today: date) -> dict:
    """`macro:data:snapshot:{today}` → dict. 실패/부재 시 빈 dict."""
    if redis_client is None:
        return {}
    key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{today.isoformat()}"
    try:
        raw = await redis_client.get(key)
    except Exception:
        logger.warning("macro snapshot Redis 조회 실패 — VIX/아시아 지수 생략", exc_info=True)
        return {}
    if raw is None:
        return {}
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        logger.warning("macro snapshot JSON 파싱 실패 — 생략")
        return {}


async def _fetch_latest_indices(conn, today: date) -> dict:
    """index_daily_prices 에서 KOSPI/KOSDAQ 최신 종가 + 전일 대비 % 를 구한다.

    change_pct 컬럼이 비어있을 수 있어 이전 영업일 close 와 직접 계산.
    7일 이상 오래된 데이터면 None (장기 미수집 상태면 차라리 숨긴다).
    """
    stmt = text(
        """
        SELECT index_code, price_date, close_price
        FROM index_daily_prices
        WHERE index_code IN ('KOSPI', 'KOSDAQ')
          AND price_date >= :since
        ORDER BY index_code, price_date DESC
        """
    )
    since = today - timedelta(days=7)
    try:
        rows = (await conn.execute(stmt, {"since": since})).mappings().all()
    except Exception:
        logger.warning("index_daily_prices 조회 실패 — KOSPI/KOSDAQ 생략", exc_info=True)
        return {}

    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["index_code"], []).append(r)

    out: dict = {}
    for code, entries in by_code.items():
        if not entries:
            continue
        latest = entries[0]
        prev_close = entries[1]["close_price"] if len(entries) > 1 else None
        change_pct: float | None = None
        if prev_close and prev_close != 0:
            change_pct = (latest["close_price"] - prev_close) / prev_close * 100.0
        if code == "KOSPI":
            out["kospi_index"] = float(latest["close_price"])
            out["kospi_change_pct"] = change_pct
        elif code == "KOSDAQ":
            out["kosdaq_index"] = float(latest["close_price"])
            out["kosdaq_change_pct"] = change_pct
    return out


async def _collect_assets(conn, today: date) -> dict | None:
    """daily_asset_snapshots 에서 최근 3일 이내 최신 snapshot.

    주말/월요일 gap(T-3) 까지만 허용 — 더 오래된 snapshot 이 "오늘 자산" 처럼
    노출되면 사용자에게 오히려 잘못된 정보.
    """
    since = today - timedelta(days=3)
    stmt = text(
        """
        SELECT snapshot_date, total_asset, cash_balance, stock_eval_amount, position_count
        FROM daily_asset_snapshots
        WHERE snapshot_date >= :since
        ORDER BY snapshot_date DESC
        LIMIT 1
        """
    )
    try:
        row = (await conn.execute(stmt, {"since": since})).mappings().first()
    except Exception:
        logger.warning("daily_asset_snapshots 조회 실패 — 자산 현황 생략", exc_info=True)
        return None
    if row is None:
        return None
    return {
        "total_asset": int(row["total_asset"] or 0),
        "cash_balance": int(row["cash_balance"] or 0),
        "stock_eval": int(row["stock_eval_amount"] or 0),
        "position_count": int(row["position_count"] or 0),
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
    """최근 24시간 news_events 상위 5건 (impact 우선, 그다음 |sentiment_score|).

    2026-04-21 전환: news_sentiments → news_events.
    """
    since = datetime.combine(today, datetime.min.time()) - timedelta(days=1)
    stmt = text(
        """
        SELECT ne.ticker, ne.sentiment_score, ne.event_type, ne.impact_level, na.title
        FROM news_events ne
        LEFT JOIN news_articles na ON na.article_id = ne.article_id
        WHERE ne.analyzed_at >= :since
        ORDER BY
          CASE ne.impact_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
          ABS(ne.sentiment_score) DESC
        LIMIT 5
        """
    )
    try:
        rows = (await conn.execute(stmt, {"since": since})).mappings().all()
    except Exception:
        logger.warning("news_events 조회 실패 — 빈 뉴스 반환", exc_info=True)
        return []
    return [
        {
            "stock_code": r["ticker"],
            "headline": (r["title"] or "")[:80],
            "score": float(r["sentiment_score"]),
            "event_type": r["event_type"],
            "impact_level": r["impact_level"],
        }
        for r in rows
    ]


__all__ = ["collect_briefing_data"]
