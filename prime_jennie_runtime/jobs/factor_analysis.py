"""주간 팩터 분석 job.

v2 원본: `/jobs/weekly-factor-analysis` (services/jobs/app.py:2376-2505).

v3 어댑터:
- `daily_quant_scores` (009_v2_trading_ops.sql, live 테이블) + `daily_prices`
  (003_price_tables.sql, v3 자체 시세) 사용. v2 의 `daily_quant_scores` /
  `stock_daily_prices` 와 1:1 의미.
- IC 계산 알고리즘은 v2 그대로 (pandas spearman). DataFrame 변환 후
  factor_names 7종 × T+5 fwd_return.
- Redis `factor:analysis:latest` (TTL 7일) JSON 키/포맷 v2 동일.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


_FACTOR_NAMES = (
    "total_quant_score",
    "momentum_score",
    "quality_score",
    "value_score",
    "technical_score",
    "news_score",
    "supply_demand_score",
)
_CACHE_KEY = "factor:analysis:latest"
_CACHE_TTL = 7 * 86400
_MIN_SAMPLES = 10
_FORWARD_DAYS = 5
_HIGH_THRESHOLD = 70


async def weekly_factor_analysis(
    pool: Any,
    redis_client: aioredis.Redis,
    *,
    period_days: int = 30,
) -> dict:
    """v2 `/jobs/weekly-factor-analysis` 포팅.

    1) daily_quant_scores 최근 30일 final_selected
    2) 종목별 T+5 forward return (daily_prices)
    3) 7개 팩터 × fwd_return 의 Spearman IC
    4) 조건부 성과: news_score≥70 / supply_demand_score≥70 / 둘 다
    5) Redis factor:analysis:latest 캐시 (TTL 7일)

    Returns: 결과 dict 또는 {"sample_count": N, "skipped": reason} 형태.
    """
    today = date.today()
    start = today - timedelta(days=period_days)

    async with pool.acquire() as conn:
        score_rows = await conn.fetch(
            "SELECT score_date, stock_code, total_quant_score, momentum_score, "
            "quality_score, value_score, technical_score, news_score, "
            "supply_demand_score "
            "FROM daily_quant_scores "
            "WHERE score_date >= $1 AND is_final_selected = TRUE",
            start,
        )

        if not score_rows:
            logger.info("weekly_factor_analysis: no quant scores in last %d days", period_days)
            return {"sample_count": 0, "skipped": "no_scores"}

        stock_codes = sorted({s["stock_code"] for s in score_rows})
        price_rows = await conn.fetch(
            "SELECT stock_code, price_date, close_price "
            "FROM daily_prices "
            "WHERE stock_code = ANY($1::text[]) AND price_date >= $2",
            stock_codes,
            start,
        )

    price_map: dict[str, dict[date, int]] = {}
    for p in price_rows:
        price_map.setdefault(p["stock_code"], {})[p["price_date"]] = int(p["close_price"])

    rows: list[dict] = []
    for s in score_rows:
        prices = price_map.get(s["stock_code"], {})
        dates_sorted = sorted(prices.keys())
        score_idx: int | None = None
        for i, d in enumerate(dates_sorted):
            if d >= s["score_date"]:
                score_idx = i
                break
        if score_idx is None or score_idx + _FORWARD_DAYS >= len(dates_sorted):
            continue

        entry_price = prices[dates_sorted[score_idx]]
        exit_idx = min(score_idx + _FORWARD_DAYS, len(dates_sorted) - 1)
        exit_price = prices[dates_sorted[exit_idx]]
        if entry_price <= 0:
            continue

        fwd_return = (exit_price - entry_price) / entry_price * 100
        row = {"fwd_return_5d": fwd_return}
        for fn in _FACTOR_NAMES:
            row[fn] = s[fn]
        rows.append(row)

    if len(rows) < _MIN_SAMPLES:
        logger.info(
            "weekly_factor_analysis: insufficient samples (%d < %d)",
            len(rows),
            _MIN_SAMPLES,
        )
        return {"sample_count": len(rows), "skipped": "insufficient_samples"}

    df = pd.DataFrame(rows)

    ic_results: dict[str, float] = {}
    for fn in _FACTOR_NAMES:
        if fn in df.columns and df[fn].notna().sum() >= _MIN_SAMPLES:
            corr = df[fn].corr(df["fwd_return_5d"], method="spearman")
            ic_results[fn] = round(float(corr) if not np.isnan(corr) else 0, 4)

    conditional: dict[str, dict] = {}
    if "news_score" in df.columns:
        high_news = df[df["news_score"] >= _HIGH_THRESHOLD]
        if not high_news.empty:
            conditional["high_news_score"] = {
                "count": len(high_news),
                "avg_return": round(float(high_news["fwd_return_5d"].mean()), 2),
                "win_rate": round(float((high_news["fwd_return_5d"] > 0).mean() * 100), 1),
            }
    if "supply_demand_score" in df.columns:
        high_supply = df[df["supply_demand_score"] >= _HIGH_THRESHOLD]
        if not high_supply.empty:
            conditional["high_supply_demand"] = {
                "count": len(high_supply),
                "avg_return": round(float(high_supply["fwd_return_5d"].mean()), 2),
                "win_rate": round(float((high_supply["fwd_return_5d"] > 0).mean() * 100), 1),
            }

    combined = df[
        (df.get("news_score", 0) >= _HIGH_THRESHOLD)
        & (df.get("supply_demand_score", 0) >= _HIGH_THRESHOLD)
    ]
    if not combined.empty:
        conditional["news_and_supply_combined"] = {
            "count": len(combined),
            "avg_return": round(float(combined["fwd_return_5d"].mean()), 2),
            "win_rate": round(float((combined["fwd_return_5d"] > 0).mean() * 100), 1),
        }

    result = {
        "sample_count": len(rows),
        "ic_spearman": ic_results,
        "conditional_performance": conditional,
        "updated_at": datetime.now().isoformat(),
    }

    await redis_client.set(
        _CACHE_KEY, json.dumps(result, ensure_ascii=False), ex=_CACHE_TTL
    )
    top = max(ic_results.items(), key=lambda x: abs(x[1]), default=("none", 0))
    logger.info(
        "weekly_factor_analysis: samples=%d top_ic=%s(%s)",
        len(rows),
        top[0],
        top[1],
    )
    return result


__all__ = ["weekly_factor_analysis"]
