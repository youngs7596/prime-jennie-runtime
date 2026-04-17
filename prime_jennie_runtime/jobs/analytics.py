"""AI 성과 분석 + 분석가 피드백 job (Redis 파이프라인).

v2 원본:
- `/jobs/analyze-ai-performance` (services/jobs/app.py:702-802)
- `/jobs/analyst-feedback` (services/jobs/app.py:805-870)

v3 어댑터:
- v2 trade_logs SQLModel SELECT → asyncpg query against `legacy_trade_logs`
  (Track A 002 마이그레이션). v3 outcomes 는 sheet 단위라 reason/score/regime
  을 직접 보유하지 않으므로 v2 분석 의미를 보존하려면 legacy 테이블을 유지.
- Redis sync → redis.asyncio. 키/TTL/페이로드 포맷은 v2 동일.
- v2 dashboard 의 ai-performance 위젯이 `ai:performance:latest` 를 그대로 읽기
  때문에 키와 JSON 구조 변경 금지.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


_PERF_KEY = "ai:performance:latest"
_PERF_TTL = 86400
_FEEDBACK_KEY = "analyst:feedback:summary"
_FEEDBACK_UPDATED_KEY = "analyst:feedback:updated_at"
_FEEDBACK_TTL = 7 * 86400


async def analyze_ai_performance(
    pool: Any,
    redis_client: aioredis.Redis,
    *,
    period_days: int = 30,
) -> dict:
    """v2 `/jobs/analyze-ai-performance` 포팅.

    최근 30일 SELL 거래를 sell_reason / market_regime / hybrid_score bin 별로 집계,
    Redis `ai:performance:latest` (TTL 86400) 에 JSON 저장.

    Returns: 집계 결과 dict (Redis 에 직렬화한 것과 동일).
    """
    cutoff_dt = datetime.combine(date.today() - timedelta(days=period_days), datetime.min.time())

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT reason, market_regime, hybrid_score, profit_pct "
            "FROM legacy_trade_logs "
            "WHERE trade_type = 'SELL' AND trade_timestamp >= $1",
            cutoff_dt,
        )

    if not rows:
        logger.info("analyze_ai_performance: no sell records in last %d days", period_days)
        result = {
            "period_days": period_days,
            "total_trades": 0,
            "win_rate": 0,
            "avg_profit_pct": 0,
            "by_reason": {},
            "by_regime": {},
            "by_score_bin": {},
            "updated_at": datetime.now().isoformat(),
        }
        await redis_client.set(_PERF_KEY, json.dumps(result, ensure_ascii=False), ex=_PERF_TTL)
        return result

    reason_stats: dict[str, dict] = {}
    regime_stats: dict[str, dict] = {}
    score_bins: dict[str, dict] = {}

    for r in rows:
        pct = float(r["profit_pct"] or 0.0)
        reason = r["reason"] or "UNKNOWN"
        regime = r["market_regime"] or "UNKNOWN"
        score = r["hybrid_score"]
        if score is None:
            bin_label = "no_score"
        elif score >= 80:
            bin_label = "80+"
        elif score >= 60:
            bin_label = "60-79"
        elif score >= 40:
            bin_label = "40-59"
        else:
            bin_label = "<40"

        for bucket, key in (
            (reason_stats, reason),
            (regime_stats, regime),
            (score_bins, bin_label),
        ):
            if key not in bucket:
                bucket[key] = {"count": 0, "wins": 0, "total_pct": 0.0}
            bucket[key]["count"] += 1
            bucket[key]["total_pct"] += pct
            if pct > 0:
                bucket[key]["wins"] += 1

    for bucket in (reason_stats, regime_stats, score_bins):
        for v in bucket.values():
            v["win_rate"] = round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0
            v["avg_pct"] = round(v["total_pct"] / v["count"], 2) if v["count"] else 0

    total_trades = len(rows)
    total_wins = sum(1 for r in rows if (float(r["profit_pct"] or 0.0)) > 0)
    total_pct = sum(float(r["profit_pct"] or 0.0) for r in rows)

    result = {
        "period_days": period_days,
        "total_trades": total_trades,
        "win_rate": round(total_wins / total_trades * 100, 1) if total_trades else 0,
        "avg_profit_pct": round(total_pct / total_trades, 2) if total_trades else 0,
        "by_reason": reason_stats,
        "by_regime": regime_stats,
        "by_score_bin": score_bins,
        "updated_at": datetime.now().isoformat(),
    }

    await redis_client.set(_PERF_KEY, json.dumps(result, ensure_ascii=False), ex=_PERF_TTL)
    logger.info(
        "analyze_ai_performance: trades=%d win_rate=%s%%",
        total_trades,
        result["win_rate"],
    )
    return result


async def analyst_feedback(redis_client: aioredis.Redis) -> str:
    """v2 `/jobs/analyst-feedback` 포팅.

    `ai:performance:latest` 를 읽어 마크다운 리포트로 변환.
    Redis `analyst:feedback:summary` + `analyst:feedback:updated_at` 에 저장 (TTL 7일).

    Returns: 생성된 마크다운 텍스트.
    Raises: RuntimeError — 선행 analyze_ai_performance 결과가 없을 때.
    """
    raw = await redis_client.get(_PERF_KEY)
    if not raw:
        raise RuntimeError(
            "No AI performance data available. Run analyze_ai_performance first."
        )
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    perf = json.loads(raw)

    lines: list[str] = [
        "# AI Trading Performance Report",
        f"**기간**: 최근 {perf.get('period_days', 30)}일",
        f"**총 거래**: {perf.get('total_trades', 0)}건",
        f"**승률**: {perf.get('win_rate', 0)}%",
        f"**평균 수익률**: {perf.get('avg_profit_pct', 0)}%",
        "",
        "## Sell Reason별 성과",
    ]
    for reason, stats in perf.get("by_reason", {}).items():
        lines.append(
            f"- **{reason}**: {stats['count']}건, "
            f"승률 {stats['win_rate']}%, 평균 {stats['avg_pct']}%"
        )

    lines.append("")
    lines.append("## 국면별 수익률")
    for regime, stats in perf.get("by_regime", {}).items():
        lines.append(
            f"- **{regime}**: {stats['count']}건, "
            f"승률 {stats['win_rate']}%, 평균 {stats['avg_pct']}%"
        )

    lines.append("")
    lines.append("## 점수 구간별 승률")
    for bin_label, stats in perf.get("by_score_bin", {}).items():
        lines.append(
            f"- **{bin_label}**: {stats['count']}건, "
            f"승률 {stats['win_rate']}%, 평균 {stats['avg_pct']}%"
        )

    lines.append("")
    lines.append("## 주요 피드백")
    win_rate = perf.get("win_rate", 0)
    if win_rate >= 60:
        lines.append("- 승률이 양호합니다. 현재 전략 유지를 권장합니다.")
    elif win_rate >= 40:
        lines.append("- 승률이 보통입니다. 저승률 전략의 비중 축소를 검토하세요.")
    else:
        lines.append("- 승률이 낮습니다. 진입 조건 강화 또는 스톱로스 조정이 필요합니다.")

    by_reason = perf.get("by_reason", {})
    worst_reason = min(
        by_reason.items(),
        key=lambda x: x[1].get("avg_pct", 0),
        default=None,
    )
    if worst_reason and worst_reason[1].get("avg_pct", 0) < 0:
        lines.append(
            f"- 가장 손실이 큰 매도 사유: **{worst_reason[0]}** "
            f"(평균 {worst_reason[1]['avg_pct']}%)"
        )

    report = "\n".join(lines)
    await redis_client.set(_FEEDBACK_KEY, report, ex=_FEEDBACK_TTL)
    await redis_client.set(
        _FEEDBACK_UPDATED_KEY, datetime.now().isoformat(), ex=_FEEDBACK_TTL
    )
    logger.info("analyst_feedback: %d lines", len(lines))
    return report


__all__ = ["analyst_feedback", "analyze_ai_performance"]
