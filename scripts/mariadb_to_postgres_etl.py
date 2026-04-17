"""MariaDB (v2) → Postgres (v3) ETL 컷오버 도구.

v3 migrations/006-010 이 생성한 라이브 테이블 + migrations/002, 010 의 legacy_*
아카이브 테이블까지 한 번에 복사한다. Phase 2.10 Track A.

**실행 주의**
- MS-01 v2 는 현재 데이터 수집 계속 (trading_flags:stop=1). 컷오버 시점에만 실행.
- v3 Postgres 가 migrations 001-010 모두 적용된 상태여야 함.
- 기본은 `--dry-run` 없이도 INSERT ON CONFLICT DO NOTHING 이라 재실행 안전.

사용:

    # 전체 테이블 ETL + row-count 검증
    uv run python -m scripts.mariadb_to_postgres_etl run

    # 특정 테이블만
    uv run python -m scripts.mariadb_to_postgres_etl run --only stock_masters,positions

    # row count/ sample value 검증만 (ETL 이후 무결성 점검)
    uv run python -m scripts.mariadb_to_postgres_etl verify

    # dry-run (read-only)
    uv run python -m scripts.mariadb_to_postgres_etl run --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from migrations._common import MigrationStats, build_insert_sql, copy_rows

logger = logging.getLogger("mariadb_to_postgres_etl")


# ─────────────────────────────────────────────────────────────────────
# 테이블 레지스트리 — v2 → v3 매핑
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TableSpec:
    v2_table: str
    v3_table: str
    columns: tuple[str, ...]
    pk: str = "id"  # ON CONFLICT target (single column 또는 composite → tuple 문자열)
    timestamp_col: str | None = None  # --since/--until 필터 컬럼
    sample_order_by: str | None = None  # verify 스팟 체크용 정렬 컬럼

    @property
    def select_sql(self) -> str:
        cols = ", ".join(self.columns)
        return f"SELECT {cols} FROM {self.v2_table}"


# v3 006-010 에서 새로 포팅된 라이브 테이블 + 002 legacy_* 아카이브.
# ORDER 는 FK 종속성 순서 (stock_masters 선).
TABLES: list[TableSpec] = [
    # ─── 마스터 ─────────────────────────────────────────────
    TableSpec(
        v2_table="stock_masters",
        v3_table="stock_masters",
        columns=(
            "stock_code",
            "stock_name",
            "market",
            "market_cap",
            "sector_naver",
            "sector_group",
            "is_active",
            "updated_at",
        ),
        pk="stock_code",
        sample_order_by="stock_code",
    ),
    TableSpec(
        v2_table="configs",
        v3_table="configs",
        columns=("config_key", "config_value", "description", "updated_at"),
        pk="config_key",
        sample_order_by="config_key",
    ),
    # ─── 시장 데이터 ────────────────────────────────────────
    TableSpec(
        v2_table="stock_investor_tradings",
        v3_table="stock_investor_tradings",
        columns=(
            "id",
            "stock_code",
            "trade_date",
            "foreign_net_buy",
            "institution_net_buy",
            "individual_net_buy",
            "foreign_holding_ratio",
        ),
        pk="id",
        timestamp_col="trade_date",
        sample_order_by="id",
    ),
    TableSpec(
        v2_table="stock_fundamentals",
        v3_table="stock_fundamentals",
        columns=(
            "stock_code",
            "trade_date",
            "per",
            "pbr",
            "roe",
            "market_cap",
            "updated_at",
        ),
        pk="(stock_code, trade_date)",
        timestamp_col="trade_date",
        sample_order_by="stock_code, trade_date",
    ),
    TableSpec(
        v2_table="stock_disclosures",
        v3_table="stock_disclosures",
        columns=(
            "id",
            "stock_code",
            "disclosure_date",
            "title",
            "report_type",
            "receipt_no",
            "corp_name",
        ),
        pk="id",
        timestamp_col="disclosure_date",
        sample_order_by="id",
    ),
    TableSpec(
        v2_table="stock_consensus",
        v3_table="stock_consensus",
        columns=(
            "stock_code",
            "trade_date",
            "forward_per",
            "forward_eps",
            "forward_roe",
            "target_price",
            "analyst_count",
            "investment_opinion",
            "source",
        ),
        pk="(stock_code, trade_date)",
        timestamp_col="trade_date",
        sample_order_by="stock_code, trade_date",
    ),
    TableSpec(
        v2_table="index_daily_prices",
        v3_table="index_daily_prices",
        columns=(
            "index_code",
            "price_date",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "change_pct",
        ),
        pk="(index_code, price_date)",
        timestamp_col="price_date",
        sample_order_by="index_code, price_date",
    ),
    TableSpec(
        v2_table="us_market_daily",
        v3_table="us_market_daily",
        columns=(
            "ticker",
            "price_date",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "change_pct",
            "updated_at",
        ),
        pk="(ticker, price_date)",
        timestamp_col="price_date",
        sample_order_by="ticker, price_date",
    ),
    # ─── Macro ─────────────────────────────────────────────
    TableSpec(
        v2_table="daily_macro_insights",
        v3_table="daily_macro_insights",
        columns=(
            "insight_date",
            "sentiment",
            "sentiment_score",
            "regime_hint",
            "sectors_to_favor",
            "sectors_to_avoid",
            "position_size_pct",
            "stop_loss_adjust_pct",
            "political_risk_level",
            "political_risk_summary",
            "vix_value",
            "vix_regime",
            "usd_krw",
            "kospi_index",
            "kosdaq_index",
            "sector_signals_json",
            "key_themes_json",
            "risk_factors_json",
            "raw_council_output_json",
            "council_cost_usd",
            "data_completeness_pct",
            "trading_reasoning",
            "council_consensus",
            "strategies_to_favor_json",
            "strategies_to_avoid_json",
            "opportunity_factors_json",
            "kospi_change_pct",
            "kosdaq_change_pct",
            "kospi_foreign_net",
            "kospi_institutional_net",
            "kospi_retail_net",
            "updated_at",
        ),
        pk="insight_date",
        timestamp_col="insight_date",
        sample_order_by="insight_date",
    ),
    TableSpec(
        v2_table="global_macro_snapshots",
        v3_table="global_macro_snapshots",
        columns=(
            "snapshot_date",
            "fed_rate",
            "treasury_2y",
            "treasury_10y",
            "treasury_spread",
            "us_cpi_yoy",
            "us_unemployment",
            "vix",
            "vix_regime",
            "dxy_index",
            "usd_krw",
            "bok_rate",
            "kospi_index",
            "kospi_change_pct",
            "kosdaq_index",
            "kosdaq_change_pct",
            "kospi_foreign_net",
            "kosdaq_foreign_net",
            "kospi_institutional_net",
            "kospi_retail_net",
            "completeness_pct",
            "data_sources_json",
            "updated_at",
        ),
        pk="snapshot_date",
        timestamp_col="snapshot_date",
        sample_order_by="snapshot_date",
    ),
    # ─── 운영 Trading ───────────────────────────────────────
    TableSpec(
        v2_table="positions",
        v3_table="positions",
        columns=(
            "stock_code",
            "stock_name",
            "quantity",
            "average_buy_price",
            "total_buy_amount",
            "sector_group",
            "high_watermark",
            "stop_loss_price",
            "created_at",
            "updated_at",
        ),
        pk="stock_code",
        sample_order_by="stock_code",
    ),
    TableSpec(
        v2_table="daily_asset_snapshots",
        v3_table="daily_asset_snapshots",
        columns=(
            "snapshot_date",
            "total_asset",
            "cash_balance",
            "stock_eval_amount",
            "total_profit_loss",
            "realized_profit_loss",
            "net_investment",
            "position_count",
        ),
        pk="snapshot_date",
        timestamp_col="snapshot_date",
        sample_order_by="snapshot_date",
    ),
    TableSpec(
        v2_table="watchlist_histories",
        v3_table="watchlist_histories",
        columns=(
            "snapshot_date",
            "stock_code",
            "run_id",
            "stock_name",
            "llm_score",
            "hybrid_score",
            "is_tradable",
            "trade_tier",
            "risk_tag",
            "rank",
            "quant_score",
            "sector_group",
            "market_regime",
            "is_active",
        ),
        pk="(snapshot_date, stock_code, run_id)",
        timestamp_col="snapshot_date",
        sample_order_by="snapshot_date, stock_code, run_id",
    ),
    TableSpec(
        v2_table="daily_quant_scores",
        v3_table="daily_quant_scores",
        columns=(
            "id",
            "score_date",
            "stock_code",
            "stock_name",
            "total_quant_score",
            "momentum_score",
            "quality_score",
            "value_score",
            "technical_score",
            "news_score",
            "supply_demand_score",
            "llm_score",
            "hybrid_score",
            "llm_grade",
            "llm_reason",
            "risk_tag",
            "trade_tier",
            "is_tradable",
            "is_final_selected",
            "run_id",
            "is_active",
        ),
        pk="id",
        timestamp_col="score_date",
        sample_order_by="id",
    ),
    # ─── 아카이브 legacy_* ─────────────────────────────────
    TableSpec(
        v2_table="signal_logs",
        v3_table="legacy_signal_logs",
        columns=(
            "id",
            "signal_type",
            "stock_code",
            "stock_name",
            "strategy",
            "price",
            "quantity",
            "hybrid_score",
            "rsi_value",
            "volume_ratio",
            "market_regime",
            "position_multiplier",
            "profit_pct",
            "holding_days",
            "status",
            "suppressed_reason",
            "created_at",
        ),
        pk="id",
        timestamp_col="created_at",
        sample_order_by="id",
    ),
    TableSpec(
        v2_table="trade_logs",
        v3_table="legacy_trade_logs",
        columns=(
            "id",
            "stock_code",
            "stock_name",
            "trade_type",
            "quantity",
            "price",
            "total_amount",
            "reason",
            "strategy_signal",
            "market_regime",
            "llm_score",
            "hybrid_score",
            "trade_tier",
            "profit_pct",
            "profit_amount",
            "holding_days",
            "trade_timestamp",
        ),
        pk="id",
        timestamp_col="trade_timestamp",
        sample_order_by="id",
    ),
    TableSpec(
        v2_table="stock_news_sentiments",
        v3_table="legacy_news_sentiments",
        columns=(
            "id",
            "stock_code",
            "news_date",
            "press",
            "headline",
            "summary",
            "sentiment_score",
            "sentiment_reason",
            "category",
            "article_url",
            "published_at",
            "source",
        ),
        pk="id",
        timestamp_col="news_date",
        sample_order_by="id",
    ),
]


TABLE_INDEX: dict[str, TableSpec] = {t.v2_table: t for t in TABLES}


# ─────────────────────────────────────────────────────────────────────
# Source / Target 어댑터 (aiomysql, asyncpg)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class MariaDBSource:
    conn: Any  # aiomysql.Connection
    spec: TableSpec

    async def count(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        where, params = _build_where(self.spec, since, until)
        sql = f"SELECT COUNT(*) FROM {self.spec.v2_table}{where}"
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            (n,) = await cur.fetchone()
            return int(n)

    async def iter_rows(
        self,
        *,
        batch_size: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        where, params = _build_where(self.spec, since, until)
        order = self.spec.sample_order_by or self.spec.columns[0]
        sql = f"{self.spec.select_sql}{where} ORDER BY {order}"
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            while True:
                rows = await cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(zip(self.spec.columns, row, strict=True))


@dataclass
class PostgresTarget:
    conn: Any  # asyncpg.Connection
    spec: TableSpec
    _insert_sql: str = field(init=False)

    def __post_init__(self) -> None:
        self._insert_sql = build_insert_sql(
            table=self.spec.v3_table,
            columns=list(self.spec.columns),
            on_conflict_pk=self.spec.pk,
        )

    async def write_batch(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        tuples = [tuple(r.get(c) for c in self.spec.columns) for r in rows]
        await self.conn.executemany(self._insert_sql, tuples)
        return len(tuples)

    async def count(self) -> int:
        row = await self.conn.fetchrow(f"SELECT COUNT(*) AS n FROM {self.spec.v3_table}")
        return int(row["n"])


def _build_where(
    spec: TableSpec,
    since: datetime | None,
    until: datetime | None,
) -> tuple[str, tuple[Any, ...]]:
    if spec.timestamp_col is None:
        return "", ()
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append(f"{spec.timestamp_col} >= %s")
        params.append(since)
    if until is not None:
        clauses.append(f"{spec.timestamp_col} < %s")
        params.append(until)
    if not clauses:
        return "", ()
    return " WHERE " + " AND ".join(clauses), tuple(params)


# ─────────────────────────────────────────────────────────────────────
# 주 런루프
# ─────────────────────────────────────────────────────────────────────


@dataclass
class TableResult:
    spec: TableSpec
    stats: MigrationStats | None = None
    v2_count: int = 0
    v3_count: int = 0
    sample_match: bool | None = None
    error: str | None = None


async def _run_table(
    args: argparse.Namespace,
    v2_conn: Any,
    v3_conn: Any,
    spec: TableSpec,
) -> TableResult:
    result = TableResult(spec=spec)
    try:
        source = MariaDBSource(conn=v2_conn, spec=spec)
        target = PostgresTarget(conn=v3_conn, spec=spec)
        result.stats = await copy_rows(
            source,
            target,
            batch_size=args.batch_size,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
        )
        result.v2_count = result.stats.source_total
        result.v3_count = await target.count()
        result.sample_match = await _verify_sample(v2_conn, v3_conn, spec)
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"
        logger.exception("table %s failed", spec.v2_table)
    return result


async def _verify_sample(v2_conn: Any, v3_conn: Any, spec: TableSpec) -> bool:
    """첫 번째 row 의 모든 컬럼이 v2↔v3 동일한지."""
    order = spec.sample_order_by or spec.columns[0]
    cols = ", ".join(spec.columns)
    async with v2_conn.cursor() as cur:
        await cur.execute(f"SELECT {cols} FROM {spec.v2_table} ORDER BY {order} LIMIT 1")
        v2_row = await cur.fetchone()
    if v2_row is None:
        return True  # 빈 테이블은 trivially match.
    v3_row = await v3_conn.fetchrow(f"SELECT {cols} FROM {spec.v3_table} ORDER BY {order} LIMIT 1")
    if v3_row is None:
        return False
    v2_dict = dict(zip(spec.columns, v2_row, strict=True))
    v3_dict = dict(v3_row)
    return _rows_equal(v2_dict, v3_dict)


def _rows_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for k in a:
        av, bv = a[k], b.get(k)
        if av is None and bv is None:
            continue
        if isinstance(av, datetime | date) or isinstance(bv, datetime | date):
            # TZ 가 한쪽에만 붙는 경우 isoformat 비교로 정규화.
            if str(av) != str(bv):
                return False
            continue
        if av != bv:
            return False
    return True


async def cmd_run(args: argparse.Namespace) -> int:
    selected = _select_tables(args)
    import aiomysql  # type: ignore[import-not-found]
    import asyncpg  # type: ignore[import-not-found]

    v2_conn = await aiomysql.connect(
        host=args.v2_host,
        port=args.v2_port,
        user=args.v2_user,
        password=args.v2_password,
        db=args.v2_db,
        charset="utf8mb4",
    )
    v3_conn = await asyncpg.connect(args.v3_dsn)

    results: list[TableResult] = []
    try:
        for spec in selected:
            logger.info("ETL %s → %s", spec.v2_table, spec.v3_table)
            results.append(await _run_table(args, v2_conn, v3_conn, spec))
    finally:
        v2_conn.close()
        await v3_conn.close()

    _print_report(results)
    return 0 if all(r.error is None for r in results) else 1


async def cmd_verify(args: argparse.Namespace) -> int:
    """ETL 이후 무결성 점검 — row count + sample row 비교."""
    selected = _select_tables(args)
    import aiomysql  # type: ignore[import-not-found]
    import asyncpg  # type: ignore[import-not-found]

    v2_conn = await aiomysql.connect(
        host=args.v2_host,
        port=args.v2_port,
        user=args.v2_user,
        password=args.v2_password,
        db=args.v2_db,
        charset="utf8mb4",
    )
    v3_conn = await asyncpg.connect(args.v3_dsn)

    results: list[TableResult] = []
    try:
        for spec in selected:
            source = MariaDBSource(conn=v2_conn, spec=spec)
            target = PostgresTarget(conn=v3_conn, spec=spec)
            v2_count = await source.count(since=args.since, until=args.until)
            v3_count = await target.count()
            match = await _verify_sample(v2_conn, v3_conn, spec)
            ok = v2_count == v3_count and match
            results.append(
                TableResult(
                    spec=spec,
                    v2_count=v2_count,
                    v3_count=v3_count,
                    sample_match=match,
                    error=None if ok else "verification mismatch",
                )
            )
    finally:
        v2_conn.close()
        await v3_conn.close()

    _print_report(results)
    return 0 if all(r.error is None for r in results) else 1


def _select_tables(args: argparse.Namespace) -> list[TableSpec]:
    if not args.only:
        return TABLES
    names = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [n for n in names if n not in TABLE_INDEX]
    if unknown:
        raise SystemExit(f"unknown tables: {unknown}. Known: {sorted(TABLE_INDEX)}")
    return [TABLE_INDEX[n] for n in names]


def _print_report(results: list[TableResult]) -> None:
    print("\n=== ETL report ===")
    print(f"{'v2_table':<32} {'v3_count':>10} {'v2_count':>10} {'sample':>8} {'status'}")
    for r in results:
        status = r.error or ("ok" if r.sample_match else "sample_mismatch")
        sample = "y" if r.sample_match else "n"
        print(f"{r.spec.v2_table:<32} {r.v3_count:>10} {r.v2_count:>10} {sample:>8} {status}")


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_datetime(s: str) -> datetime:
    if len(s) == 10:
        return datetime.fromisoformat(s + "T00:00:00")
    return datetime.fromisoformat(s)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MariaDB (v2) → Postgres (v3) ETL cutover")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--only", default="", help="comma-separated v2 table names")
    common.add_argument("--batch-size", type=int, default=1000)
    common.add_argument("--since", type=_parse_datetime, default=None)
    common.add_argument("--until", type=_parse_datetime, default=None)
    common.add_argument("--v2-host", default="localhost")
    common.add_argument("--v2-port", type=int, default=3307)
    common.add_argument("--v2-user", default="prime")
    common.add_argument("--v2-password", default="")
    common.add_argument("--v2-db", default="prime_jennie")
    common.add_argument(
        "--v3-dsn",
        default="postgresql://pj_runtime:dev_password@localhost:5432/prime_jennie_v3",
    )

    p_run = sub.add_parser("run", parents=[common], help="실 ETL 실행")
    p_run.add_argument("--dry-run", action="store_true", help="read-only 검증")

    sub.add_parser("verify", parents=[common], help="row count + sample row 비교")
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "run":
        return await cmd_run(args)
    if args.cmd == "verify":
        # verify 는 dry_run 가 의미 없음. 빈 속성으로 채움 (dry_run 미참조).
        args.dry_run = False
        return await cmd_verify(args)
    raise SystemExit(f"unknown command: {args.cmd}")


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
