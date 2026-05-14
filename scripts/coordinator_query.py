"""Coordinator event_log / decision_log 조회 CLI.

Stage 1.5 (Agent Coordinator) 검증 / 운영용 헬퍼. event_log 와 decision_log 를
사람이 읽기 쉬운 행 단위 출력으로 보여준다.

사용 예:
    python -m scripts.coordinator_query --kind sheet_published --limit 10
    python -m scripts.coordinator_query --ticker 005930 --since 2026-05-14T00:00:00
    python -m scripts.coordinator_query --mode decisions --kind entry_decision
    python -m scripts.coordinator_query --correlation-id sheet-xyz

design: .ai/designs/2026-05-14-agent-coordinator.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import asyncpg

logger = logging.getLogger("coordinator_query")


# ─────────────────────────────────────────────────────────────────────
# DSN — scripts/backfill_daily_prices.py 와 동일한 env 패턴
# ─────────────────────────────────────────────────────────────────────


def dsn_from_env() -> str:
    user = os.environ.get("POSTGRES_USER", "pj_runtime")
    password = os.environ.get("POSTGRES_PASSWORD", "dev_password")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "prime_jennie_v3")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# ─────────────────────────────────────────────────────────────────────
# Query builder
# ─────────────────────────────────────────────────────────────────────


_EVENT_COLUMNS = (
    "event_id",
    "event_kind",
    "subject_id",
    "ticker",
    "correlation_id",
    "decision_id",
    "occurred_at",
    "recorded_at",
    "payload_json",
)

_DECISION_COLUMNS = (
    "decision_id",
    "kind",
    "subject_id",
    "outcome",
    "reason",
    "policy_name",
    "decided_at",
    "created_at",
    "context_snapshot_json",
)


def _build_query(
    *,
    mode: str,
    kind: str | None,
    ticker: str | None,
    correlation_id: str | None,
    since: datetime,
    limit: int,
) -> tuple[str, list]:
    """Return (sql, params). placeholder 1-based asyncpg style."""
    if mode == "events":
        cols = ", ".join(_EVENT_COLUMNS)
        sql = f"SELECT {cols} FROM event_log"
        kind_col = "event_kind"
        time_col = "occurred_at"
        ticker_supported = True
        correlation_supported = True
    elif mode == "decisions":
        cols = ", ".join(_DECISION_COLUMNS)
        sql = f"SELECT {cols} FROM decision_log"
        kind_col = "kind"
        time_col = "decided_at"
        ticker_supported = False  # decision_log 엔 ticker 컬럼 없음
        correlation_supported = False
    else:
        raise ValueError(f"unknown mode: {mode}")

    where: list[str] = []
    params: list = []
    if kind:
        params.append(kind)
        where.append(f"{kind_col} = ${len(params)}")
    if ticker:
        if not ticker_supported:
            logger.warning("--ticker 는 mode=events 에서만 의미가 있음 — decisions 모드에서 무시")
        else:
            params.append(ticker)
            where.append(f"ticker = ${len(params)}")
    if correlation_id:
        if not correlation_supported:
            logger.warning(
                "--correlation-id 는 mode=events 에서만 의미가 있음 — decisions 모드에서 무시"
            )
        else:
            params.append(correlation_id)
            where.append(f"correlation_id = ${len(params)}")
    params.append(since)
    where.append(f"{time_col} >= ${len(params)}")

    sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {time_col} DESC"
    params.append(limit)
    sql += f" LIMIT ${len(params)}"
    return sql, params


# ─────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def _truncate(s: str, limit: int = 60) -> str:
    s = s.replace("\n", " ")
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _render_events(rows: Sequence[asyncpg.Record]) -> str:
    if not rows:
        return "(no event rows)"
    header = (
        f"{'event_id':>8}  {'occurred_at':<26}  {'event_kind':<18}  "
        f"{'ticker':<8}  {'subject_id':<24}  {'correlation_id':<24}  payload"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        payload = r["payload_json"]
        if isinstance(payload, str):
            try:
                payload_obj = json.loads(payload)
            except json.JSONDecodeError:
                payload_obj = {"_raw": payload}
        else:
            payload_obj = payload
        payload_str = _truncate(json.dumps(payload_obj, ensure_ascii=False, default=str), 80)
        lines.append(
            f"{r['event_id']:>8}  {_fmt_ts(r['occurred_at']):<26}  "
            f"{(r['event_kind'] or '-'):<18}  "
            f"{(r['ticker'] or '-'):<8}  "
            f"{_truncate(r['subject_id'] or '-', 24):<24}  "
            f"{_truncate(r['correlation_id'] or '-', 24):<24}  "
            f"{payload_str}"
        )
    return "\n".join(lines)


def _render_decisions(rows: Sequence[asyncpg.Record]) -> str:
    if not rows:
        return "(no decision rows)"
    header = (
        f"{'decision_id':>11}  {'decided_at':<26}  {'kind':<22}  "
        f"{'outcome':<18}  {'subject_id':<24}  {'policy_name':<24}  reason"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['decision_id']:>11}  {_fmt_ts(r['decided_at']):<26}  "
            f"{_truncate(r['kind'] or '-', 22):<22}  "
            f"{_truncate(r['outcome'] or '-', 18):<18}  "
            f"{_truncate(r['subject_id'] or '-', 24):<24}  "
            f"{_truncate(r['policy_name'] or '-', 24):<24}  "
            f"{_truncate(r['reason'] or '-', 60)}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def _parse_since(value: str | None) -> datetime:
    """ISO datetime 파서. 기본은 24h 전 (UTC)."""
    if value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as e:
            raise SystemExit(f"--since ISO datetime 파싱 실패: {value} ({e})") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return datetime.now(UTC) - timedelta(hours=24)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coordinator_query",
        description="Coordinator event_log / decision_log 조회",
    )
    p.add_argument(
        "--mode",
        choices=("events", "decisions"),
        default="events",
        help="조회 대상 테이블 (default: events)",
    )
    p.add_argument("--kind", default=None, help="event_kind 또는 decision kind 필터")
    p.add_argument("--ticker", default=None, help="ticker 필터 (events 모드만)")
    p.add_argument("--correlation-id", default=None, help="correlation_id 필터 (events 모드만)")
    p.add_argument(
        "--since",
        default=None,
        help="ISO datetime — 이 시각 이후만 (default: 24h 전)",
    )
    p.add_argument("--limit", type=int, default=50, help="최대 row 수 (default: 50)")
    p.add_argument("--dsn", default=None, help="Postgres DSN override")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG 로그",
    )
    return p


async def run(args: argparse.Namespace) -> int:
    dsn = args.dsn or dsn_from_env()
    since = _parse_since(args.since)
    sql, params = _build_query(
        mode=args.mode,
        kind=args.kind,
        ticker=args.ticker,
        correlation_id=args.correlation_id,
        since=since,
        limit=args.limit,
    )
    logger.debug("dsn=%s sql=%s params=%s", dsn, sql, params)
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        logger.error("Postgres 연결 실패: %s", e)
        return 2

    try:
        rows = await conn.fetch(sql, *params)
    finally:
        await conn.close()

    print(f"# mode={args.mode} since={since.isoformat()} matched={len(rows)} limit={args.limit}")
    if args.mode == "events":
        print(_render_events(rows))
    else:
        print(_render_decisions(rows))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
