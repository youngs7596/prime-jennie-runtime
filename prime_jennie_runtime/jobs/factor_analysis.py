"""주간 팩터 분석 job — 유니버스 전체 IC + 다구간 + 이력 영속.

v2 원본: `/jobs/weekly-factor-analysis` (services/jobs/app.py:2376-2505).

v3 어댑터:
- `daily_quant_scores` (009_v2_trading_ops.sql, live 테이블) + `daily_prices`
  (003_price_tables.sql, v3 자체 시세) 사용.
- IC 계산은 pandas Spearman (v2 동일). factor 별 vs forward return.
- Redis `factor:analysis:latest` (TTL 7일) 캐시 유지.

2026-06-18 개편 — paper 선별 진단에서 드러난 네 공백을 메운다:
- 기존엔 ``is_final_selected = TRUE`` 종목만 봤다. 이미 고른 종목은 점수 분산이
  거의 없어 IC 가 무의미했다 (range restriction). 유니버스 전체(채점된 전 종목)로
  IC 를 잰다. 비교용으로 선정 종목(scope='selected')도 같이 낸다.
- forward 구간을 T+5 단독 → T+5/T+10 (전략별 보유기간 차이).
- sector_momentum_score 팩터 추가 (migration 025 로 컬럼 생김. 과거 행은 NULL →
  표본 충분해질 때까지 자동 skip).
- IC 결과를 ``factor_ic_history`` 에 영속 (Redis 캐시에 더해). 팩터 예측력의
  시간 변화를 추적 — slow_loop 선별 개선의 ground truth.
- 하루 7 run 스냅샷은 (score_date, stock_code) 마지막 run 1건으로 dedup.
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
    "sector_momentum_score",
)
_HORIZONS = (5, 10)
_SCOPES = ("universe", "selected")
_CACHE_KEY = "factor:analysis:latest"
_CACHE_TTL = 7 * 86400
_MIN_SAMPLES = 10
_HIGH_THRESHOLD = 70


async def weekly_factor_analysis(
    pool: Any,
    redis_client: aioredis.Redis,
    *,
    period_days: int = 30,
) -> dict:
    """유니버스 전체 팩터 IC 분석 — scope × horizon × factor.

    1) daily_quant_scores 최근 ``period_days`` 일 전 종목 (run 당 마지막 1건 dedup)
    2) 종목별 T+5 / T+10 forward return (daily_prices)
    3) 8개 팩터 × fwd_return 의 Spearman IC — universe / selected 두 범위
    4) 조건부 성과: news_score≥70 / supply_demand_score≥70 / 둘 다 (universe, T+5)
    5) factor_ic_history 영속 + Redis factor:analysis:latest 캐시 (TTL 7일)

    Returns: 결과 dict 또는 {"sample_count": N, "skipped": reason} 형태.
    """
    today = date.today()
    start = today - timedelta(days=period_days)

    async with pool.acquire() as conn:
        score_rows = await conn.fetch(
            "SELECT DISTINCT ON (score_date, stock_code) "
            "score_date, stock_code, total_quant_score, momentum_score, "
            "quality_score, value_score, technical_score, news_score, "
            "supply_demand_score, sector_momentum_score, is_final_selected "
            "FROM daily_quant_scores "
            "WHERE score_date >= $1 "
            "ORDER BY score_date, stock_code, run_id DESC",
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

    price_map: dict[str, dict[date, float]] = {}
    for p in price_rows:
        price_map.setdefault(p["stock_code"], {})[p["price_date"]] = float(p["close_price"])

    # 각 스냅샷 행 → 팩터값 + 구간별 forward return. entry = score_date 이후 첫
    # 거래일 종가 (= 같은 날 종가), exit = entry 로부터 horizon 거래일 뒤 종가.
    rows: list[dict] = []
    for s in score_rows:
        prices = price_map.get(s["stock_code"], {})
        dates_sorted = sorted(prices.keys())
        score_idx: int | None = None
        for i, d in enumerate(dates_sorted):
            if d >= s["score_date"]:
                score_idx = i
                break
        if score_idx is None:
            continue
        entry_price = prices[dates_sorted[score_idx]]
        if entry_price <= 0:
            continue

        row: dict = {"is_final_selected": bool(s["is_final_selected"])}
        for fn in _FACTOR_NAMES:
            row[fn] = s[fn]

        has_fwd = False
        for h in _HORIZONS:
            exit_idx = score_idx + h
            if exit_idx >= len(dates_sorted):
                continue  # 아직 윈도우가 안 닫힘 — 이 horizon 만 skip
            exit_price = prices[dates_sorted[exit_idx]]
            row[f"fwd_{h}"] = (exit_price - entry_price) / entry_price * 100
            has_fwd = True
        if has_fwd:
            rows.append(row)

    if len(rows) < _MIN_SAMPLES:
        logger.info(
            "weekly_factor_analysis: insufficient samples (%d < %d)",
            len(rows),
            _MIN_SAMPLES,
        )
        return {"sample_count": len(rows), "skipped": "insufficient_samples"}

    df = pd.DataFrame(rows)

    # IC — scope(universe/selected) × horizon × factor
    by_scope: dict[str, dict] = {}
    ic_records: list[dict] = []
    scope_frames = {"universe": df, "selected": df[df["is_final_selected"]]}
    for scope in _SCOPES:
        sdf = scope_frames[scope]
        by_horizon: dict[str, dict] = {}
        for h in _HORIZONS:
            col = f"fwd_{h}"
            if col not in sdf.columns:
                continue
            hdf = sdf[sdf[col].notna()]
            ic_results: dict[str, float] = {}
            for fn in _FACTOR_NAMES:
                if fn in hdf.columns and hdf[fn].notna().sum() >= _MIN_SAMPLES:
                    corr = hdf[fn].corr(hdf[col], method="spearman")
                    ic = round(float(corr) if not np.isnan(corr) else 0.0, 4)
                    ic_results[fn] = ic
                    ic_records.append(
                        {
                            "horizon_days": h,
                            "scope": scope,
                            "factor_name": fn,
                            "ic_spearman": ic,
                            "sample_count": int(hdf[fn].notna().sum()),
                        }
                    )
            by_horizon[f"t{h}"] = {
                "sample_count": int(len(hdf)),
                "ic_spearman": ic_results,
            }
        by_scope[scope] = by_horizon

    conditional = _conditional_performance(df)

    await _persist_ic_history(
        pool, analysis_date=today, period_days=period_days, ic_records=ic_records
    )

    # 최상위 ic_spearman 은 universe T+5 (하위호환 + 가장 자주 보는 값).
    universe_t5 = by_scope.get("universe", {}).get("t5", {}).get("ic_spearman", {})
    result = {
        "sample_count": len(rows),
        "analysis_date": today.isoformat(),
        "period_days": period_days,
        "horizons": list(_HORIZONS),
        "by_scope": by_scope,
        "ic_spearman": universe_t5,
        "conditional_performance": conditional,
        "updated_at": datetime.now().isoformat(),
    }

    await redis_client.set(_CACHE_KEY, json.dumps(result, ensure_ascii=False), ex=_CACHE_TTL)
    top = max(universe_t5.items(), key=lambda x: abs(x[1]), default=("none", 0))
    logger.info(
        "weekly_factor_analysis: samples=%d universe_t5_top_ic=%s(%s) ic_rows=%d",
        len(rows),
        top[0],
        top[1],
        len(ic_records),
    )
    return result


def _conditional_performance(df: pd.DataFrame) -> dict:
    """high news / high supply_demand 조건부 성과 (universe, T+5). v2 연속성 유지."""
    col = "fwd_5"
    conditional: dict[str, dict] = {}
    if col not in df.columns:
        return conditional
    base = df[df[col].notna()]
    if base.empty:
        return conditional

    for name, factor in (
        ("high_news_score", "news_score"),
        ("high_supply_demand", "supply_demand_score"),
    ):
        if factor not in base.columns:
            continue
        high = base[base[factor] >= _HIGH_THRESHOLD]
        if not high.empty:
            conditional[name] = {
                "count": int(len(high)),
                "avg_return": round(float(high[col].mean()), 2),
                "win_rate": round(float((high[col] > 0).mean() * 100), 1),
            }

    if "news_score" in base.columns and "supply_demand_score" in base.columns:
        combined = base[
            (base["news_score"] >= _HIGH_THRESHOLD)
            & (base["supply_demand_score"] >= _HIGH_THRESHOLD)
        ]
        if not combined.empty:
            conditional["news_and_supply_combined"] = {
                "count": int(len(combined)),
                "avg_return": round(float(combined[col].mean()), 2),
                "win_rate": round(float((combined[col] > 0).mean() * 100), 1),
            }
    return conditional


async def _persist_ic_history(
    pool: Any,
    *,
    analysis_date: date,
    period_days: int,
    ic_records: list[dict],
) -> None:
    """IC 결과를 factor_ic_history 에 upsert. 같은 날 재실행이면 덮어쓴다."""
    if not ic_records:
        return
    sql = """
        INSERT INTO factor_ic_history (
            analysis_date, period_days, horizon_days, scope, factor_name,
            ic_spearman, sample_count
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (analysis_date, period_days, horizon_days, scope, factor_name)
        DO UPDATE SET
            ic_spearman = EXCLUDED.ic_spearman,
            sample_count = EXCLUDED.sample_count,
            computed_at = NOW()
    """
    try:
        async with pool.acquire() as conn:
            for r in ic_records:
                await conn.execute(
                    sql,
                    analysis_date,
                    period_days,
                    r["horizon_days"],
                    r["scope"],
                    r["factor_name"],
                    r["ic_spearman"],
                    r["sample_count"],
                )
        logger.info("factor_ic_history upsert: %d rows (date=%s)", len(ic_records), analysis_date)
    except Exception:
        logger.exception("_persist_ic_history 실패 — IC 이력 누락 가능")


__all__ = ["weekly_factor_analysis"]
