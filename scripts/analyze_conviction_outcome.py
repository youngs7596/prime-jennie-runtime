"""Scout conviction × outcome (pnl_pct) 상관관계 retrospective 분석.

Phase 0 #1 (design `.ai/designs/2026-05-14-agent-coordinator.md` §10) 의
Coordinator 도입과 독립적으로 즉시 실행 가능한 retrospective script.

가설:
  r > 0.3   → conviction 을 sizing 에 반영
  0.1~0.3   → 임계값 필터로만 활용
  -0.1~0.1  → conviction 무시 (발행 빈도 자체 재검토)
  < -0.1    → 역신호. Scout 정책 재설계

데이터 소스 (Coordinator 도입 전):
  - screening_candidates.conviction × outcomes.pnl_pct (via promoted_to_sheet_id)
  - hour_of_day: screening_candidates.created_at (KST)
  - Nth-in-window: 같은 ticker 가 윈도 안 N 번째 발행 (B1 self-reinforcing)

분석 dimensions (design §10.3):
  r1  전체
  r2  strategy_tag 별
  r5  hour_of_day 별 (시초 09:00~ 등 시간대)
  r6  Nth recommendation in window (1st / 2nd / 3rd+)

사용 예:

    uv run python -m scripts.analyze_conviction_outcome
    uv run python -m scripts.analyze_conviction_outcome --since 2026-04-01
    uv run python -m scripts.analyze_conviction_outcome --output csv
    uv run python -m scripts.analyze_conviction_outcome --strategy-tag GAP_UP

운영 DB 는 MS-01 이므로 실제 돌릴 땐 컨테이너 안에서:

    docker compose exec job-worker \\
        uv run python -m scripts.analyze_conviction_outcome --since 2026-04-01
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.infra.config import PostgresConfig
from prime_jennie_runtime.infra.db import create_engine

logger = logging.getLogger("analyze_conviction_outcome")

# Default min_n: design 본문 권고 N=30 (Pearson r 의 안정 구간 시작점).
DEFAULT_MIN_N = 30
DEFAULT_WEEKS = 6


# ---------------------------------------------------------------------------
# SQL — 핵심 join.
#
# screening_candidates (sc) 는 raw 발행 후보. promoted_to_sheet_id 가 있을 때만
# 시트 승격 → 매매 가능. outcomes 는 시트당 1행. pnl_pct 이미 계산됨
# (migrations/001_v3_schema.sql:71). 매수/매도 시각은 executions 에서.
#
# sheet_id 당 buy/sell 이 1건씩일 가능성이 높지만 scale-out 등 다중 sell 도
# 가능하므로 MIN(executed_at) FILTER (side='buy') 와 MAX(executed_at)
# FILTER (side='sell') 로 보수적으로 첫 매수 / 마지막 매도 시각 산출.
#
# pnl_pct 자체는 outcomes 의 값을 그대로 신뢰 (design SQL 의 (sell-buy)/buy
# 보다 정확 — 수수료/슬리피지 반영).
# ---------------------------------------------------------------------------

_BASE_SQL = """
WITH sheet_outcomes AS (
    SELECT
        sc.scout_run_id,
        sc.ticker,
        sc.conviction::float            AS conviction,
        sc.strategy_tag,
        sc.created_at                   AS published_at,
        sc.promoted_to_sheet_id         AS sheet_id,
        ex.first_buy_at,
        o.pnl_pct::float                AS pnl_pct,
        o.exit_reason,
        o.closed_at
    FROM screening_candidates sc
    JOIN outcomes o
      ON o.sheet_id = sc.promoted_to_sheet_id
    LEFT JOIN (
        SELECT
            sheet_id,
            MIN(executed_at) FILTER (WHERE side = 'buy')  AS first_buy_at,
            MAX(executed_at) FILTER (WHERE side = 'sell') AS last_sell_at
        FROM executions
        GROUP BY sheet_id
    ) ex ON ex.sheet_id = sc.promoted_to_sheet_id
    WHERE sc.created_at >= :since
      AND sc.created_at <  :until
      AND sc.promoted_to_sheet_id IS NOT NULL
      AND o.pnl_pct IS NOT NULL
      AND sc.conviction IS NOT NULL
)
SELECT *
FROM sheet_outcomes
ORDER BY published_at ASC;
"""


@dataclass
class CorrResult:
    """단일 dimension 의 Pearson r 결과."""

    label: str
    r: float | None
    n: int
    note: str = ""

    @property
    def is_low_confidence(self) -> bool:
        return self.r is None or self.n < self._min_n

    _min_n: int = field(default=DEFAULT_MIN_N, repr=False)


def _pearson(values_x: Iterable[float], values_y: Iterable[float]) -> tuple[float | None, int]:
    """Pearson r 계산. 표본 < 2 또는 분산 0 이면 (None, n).

    scipy.stats.pearsonr 가 ConstantInputWarning 을 띄우면 None 반환.
    """
    x = np.asarray(list(values_x), dtype=float)
    y = np.asarray(list(values_y), dtype=float)
    # NaN 제거 (예: pnl_pct 계산 실패 행).
    mask = ~(np.isnan(x) | np.isnan(y))
    x = x[mask]
    y = y[mask]
    n = int(x.size)
    if n < 2:
        return None, n
    # 분산 0 이면 상관 정의 불가.
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return None, n
    r, _p = stats.pearsonr(x, y)
    return float(r), n


def _compute_r1(df: pd.DataFrame, min_n: int) -> CorrResult:
    r, n = _pearson(df["conviction"], df["pnl_pct"])
    return CorrResult(label="overall", r=r, n=n, _min_n=min_n)


def _compute_by_group(
    df: pd.DataFrame,
    group_col: str,
    min_n: int,
    *,
    sort_by_label: bool = True,
) -> list[CorrResult]:
    results: list[CorrResult] = []
    for label, sub in df.groupby(group_col, dropna=False):
        r, n = _pearson(sub["conviction"], sub["pnl_pct"])
        results.append(CorrResult(label=str(label), r=r, n=n, _min_n=min_n))
    if sort_by_label:
        results.sort(key=lambda x: x.label)
    return results


def _compute_by_hour(df: pd.DataFrame, min_n: int, tz: str = "Asia/Seoul") -> list[CorrResult]:
    """발행 시각 (KST) 의 hour-of-day 별 r."""
    if df.empty:
        return []
    # published_at 은 TIMESTAMPTZ → asyncpg 가 UTC aware datetime 반환.
    # KST 로 변환 후 hour 추출.
    ts = pd.to_datetime(df["published_at"], utc=True).dt.tz_convert(tz)
    hours = ts.dt.hour
    work = df.assign(_hour=hours)
    results: list[CorrResult] = []
    for hour, sub in work.groupby("_hour"):
        r, n = _pearson(sub["conviction"], sub["pnl_pct"])
        results.append(
            CorrResult(label=f"{int(hour):02d}:00~{int(hour):02d}:59", r=r, n=n, _min_n=min_n)
        )
    results.sort(key=lambda x: x.label)
    return results


def _compute_nth_in_window(df: pd.DataFrame, min_n: int) -> list[CorrResult]:
    """같은 ticker 가 윈도(=df) 안에서 발행된 N 번째 추천 별 r.

    1st / 2nd / 3rd+ 의 3 버킷 (design §10.3 r6 — B1 self-reinforcing 정량).
    """
    if df.empty:
        return []
    work = df.sort_values("published_at").copy()
    work["nth"] = work.groupby("ticker").cumcount() + 1

    def _bucket(n: int) -> str:
        if n == 1:
            return "1st"
        if n == 2:
            return "2nd"
        return "3rd+"

    work["bucket"] = work["nth"].apply(_bucket)
    results: list[CorrResult] = []
    for bucket in ("1st", "2nd", "3rd+"):
        sub = work[work["bucket"] == bucket]
        r, n = _pearson(sub["conviction"], sub["pnl_pct"])
        results.append(CorrResult(label=bucket, r=r, n=n, _min_n=min_n))
    return results


# ---------------------------------------------------------------------------
# DB load
# ---------------------------------------------------------------------------


async def _load(
    engine: AsyncEngine,
    *,
    since: datetime,
    until: datetime,
    strategy_tag: str | None,
) -> pd.DataFrame:
    sql = _BASE_SQL
    params: dict[str, object] = {"since": since, "until": until}
    if strategy_tag:
        # 안전: parametric. _BASE_SQL 의 WHERE 끝에 AND 절 추가.
        sql = sql.replace(
            "AND sc.conviction IS NOT NULL\n",
            "AND sc.conviction IS NOT NULL\n      AND sc.strategy_tag = :strategy_tag\n",
        )
        params["strategy_tag"] = strategy_tag

    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        rows = [dict(r) for r in result.mappings().all()]

    if not rows:
        return pd.DataFrame(
            columns=[
                "scout_run_id",
                "ticker",
                "conviction",
                "strategy_tag",
                "published_at",
                "sheet_id",
                "first_buy_at",
                "pnl_pct",
                "exit_reason",
                "closed_at",
            ]
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _fmt_r(r: float | None) -> str:
    if r is None:
        return "  n/a "
    sign = "+" if r >= 0 else "-"
    return f"{sign}{abs(r):.3f}"


def _render_table(
    *,
    since: datetime,
    until: datetime,
    min_n: int,
    strategy_tag: str | None,
    r1: CorrResult,
    r2: list[CorrResult],
    r5: list[CorrResult],
    r6: list[CorrResult],
) -> str:
    out = io.StringIO()
    weeks = (until - since).days / 7.0
    print("=== conviction x pnl_pct correlation ===", file=out)
    print(
        f"period: {since.date()} ~ {until.date()} ({weeks:.1f} weeks) "
        f"min_n={min_n}" + (f" strategy_tag={strategy_tag}" if strategy_tag else ""),
        file=out,
    )
    print("", file=out)

    warn = "  [low-n]" if r1.is_low_confidence else ""
    print(f"r1 (overall): r={_fmt_r(r1.r)}  n={r1.n}{warn}", file=out)
    print("", file=out)

    print("r2 (per strategy_tag):", file=out)
    if not r2:
        print("  (no rows)", file=out)
    for c in r2:
        w = "  [low-n]" if c.is_low_confidence else ""
        print(f"  {c.label:<14} r={_fmt_r(c.r)}  n={c.n}{w}", file=out)
    print("", file=out)

    print("r5 (per hour_of_day, KST):", file=out)
    if not r5:
        print("  (no rows)", file=out)
    for c in r5:
        w = "  [low-n]" if c.is_low_confidence else ""
        print(f"  {c.label:<14} r={_fmt_r(c.r)}  n={c.n}{w}", file=out)
    print("", file=out)

    print("r6 (Nth recommendation in window, B1 self-reinforcing):", file=out)
    if not r6:
        print("  (no rows)", file=out)
    for c in r6:
        w = "  [low-n]" if c.is_low_confidence else ""
        print(f"  {c.label:<14} r={_fmt_r(c.r)}  n={c.n}{w}", file=out)
    print("", file=out)

    print(
        "해석 가이드: r > 0.3 sizing 반영 / 0.1~0.3 임계 필터 / -0.1~0.1 무신호 / < -0.1 역신호",
        file=out,
    )
    print(
        "주의: n 이 min_n 미만인 항목 ([low-n]) 은 표본 부족 — 결론을 단정짓지 말 것.",
        file=out,
    )
    return out.getvalue()


def _render_csv(
    *,
    r1: CorrResult,
    r2: list[CorrResult],
    r5: list[CorrResult],
    r6: list[CorrResult],
) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["dimension", "label", "r", "n", "low_confidence"])
    for c in [r1, *r2, *r5, *r6]:
        dim = (
            "r1_overall"
            if c is r1
            else ("r2_strategy_tag" if c in r2 else ("r5_hour_of_day" if c in r5 else "r6_nth"))
        )
        w.writerow(
            [
                dim,
                c.label,
                "" if c.r is None else f"{c.r:.6f}",
                c.n,
                int(c.is_low_confidence),
            ]
        )
    return out.getvalue()


def _render_json(
    *,
    since: datetime,
    until: datetime,
    min_n: int,
    strategy_tag: str | None,
    r1: CorrResult,
    r2: list[CorrResult],
    r5: list[CorrResult],
    r6: list[CorrResult],
) -> str:
    def _ser(c: CorrResult) -> dict:
        return {
            "label": c.label,
            "r": c.r,
            "n": c.n,
            "low_confidence": c.is_low_confidence,
        }

    payload = {
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "min_n": min_n,
        "strategy_tag": strategy_tag,
        "r1_overall": _ser(r1),
        "r2_strategy_tag": [_ser(c) for c in r2],
        "r5_hour_of_day": [_ser(c) for c in r5],
        "r6_nth_in_window": [_ser(c) for c in r6],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """ISO date or datetime → tz-aware UTC datetime."""
    # 날짜만 들어오면 (YYYY-MM-DD) → 00:00 UTC 로 해석.
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=UTC)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid ISO date/datetime: {s!r}") from e


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze_conviction_outcome",
        description=(
            "Scout conviction vs outcome (pnl_pct) Pearson r — retrospective. "
            "design: .ai/designs/2026-05-14-agent-coordinator.md §10."
        ),
    )
    p.add_argument(
        "--since",
        type=_parse_iso,
        default=None,
        help="ISO date/datetime. default: 6 weeks ago (UTC).",
    )
    p.add_argument(
        "--until",
        type=_parse_iso,
        default=None,
        help="ISO date/datetime. default: now (UTC).",
    )
    p.add_argument(
        "--strategy-tag",
        type=str,
        default=None,
        help="restrict to one strategy_tag (e.g. GAP_UP).",
    )
    p.add_argument(
        "--min-n",
        type=int,
        default=DEFAULT_MIN_N,
        help=f"confidence threshold; n<min_n is flagged [low-n]. default {DEFAULT_MIN_N}.",
    )
    p.add_argument(
        "--output",
        choices=("table", "csv", "json"),
        default="table",
        help="output format. default table.",
    )
    return p


async def _amain(args: argparse.Namespace) -> int:
    now = datetime.now(tz=UTC)
    since = args.since or (now - timedelta(weeks=DEFAULT_WEEKS))
    until = args.until or now
    if since >= until:
        logger.error("--since (%s) must be earlier than --until (%s)", since, until)
        return 2

    cfg = PostgresConfig()
    engine = create_engine(cfg)

    try:
        df = await _load(engine, since=since, until=until, strategy_tag=args.strategy_tag)
    finally:
        await engine.dispose()

    if df.empty:
        # 빈 결과여도 cleanly 출력 — 운영 자동화에서 fail-soft 위해 0 반환.
        empty = CorrResult(label="overall", r=None, n=0, _min_n=args.min_n)
        rendered = _render_table(
            since=since,
            until=until,
            min_n=args.min_n,
            strategy_tag=args.strategy_tag,
            r1=empty,
            r2=[],
            r5=[],
            r6=[],
        )
        sys.stdout.write(rendered)
        sys.stdout.write("\n(주의: 결과 0행 — promoted_to_sheet_id 있고 outcome 닫힌 표본 없음)\n")
        return 0

    r1 = _compute_r1(df, args.min_n)
    r2 = _compute_by_group(df, "strategy_tag", args.min_n)
    r5 = _compute_by_hour(df, args.min_n)
    r6 = _compute_nth_in_window(df, args.min_n)

    if args.output == "csv":
        sys.stdout.write(_render_csv(r1=r1, r2=r2, r5=r5, r6=r6))
    elif args.output == "json":
        sys.stdout.write(
            _render_json(
                since=since,
                until=until,
                min_n=args.min_n,
                strategy_tag=args.strategy_tag,
                r1=r1,
                r2=r2,
                r5=r5,
                r6=r6,
            )
            + "\n"
        )
    else:
        sys.stdout.write(
            _render_table(
                since=since,
                until=until,
                min_n=args.min_n,
                strategy_tag=args.strategy_tag,
                r1=r1,
                r2=r2,
                r5=r5,
                r6=r6,
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _build_parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
