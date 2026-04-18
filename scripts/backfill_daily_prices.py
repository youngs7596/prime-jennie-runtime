"""daily_prices 일회성 backfill (Phase 2.10 보조 — Scout market_data 근거 채우기).

`stock_masters` 상위 시가총액 N 종목에 대해 KIS Gateway 의 `/api/market/daily-prices`
를 연속 호출해 `daily_prices` 에 upsert. ``v2 collect_minute_chart / daily_market_data
_collector`` DAG 와 별개로, 월요일 장 시작 전 한 번만 돌린다.

**실행 환경**: docker compose exec 로 컨테이너 안에서 실행해야 ``http://kis-gateway:8080``
DNS 가 풀린다. 호스트 WSL 에서는 `--gateway-url http://localhost:8080` 로 대체 가능.

추천 명령 (MS-01):

    docker compose exec price-scheduler \\
        uv run python -m scripts.backfill_daily_prices --limit 200 --days 60

Rate limit 은 KIS paper 기준 2/sec 에 맞춰 ``--sleep 0.5`` 가 기본. 실계좌 19/sec 환경
에서는 ``--sleep 0.06`` 으로 낮춰도 된다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
import httpx

from prime_jennie_runtime.kis_gateway.price_repo import PostgresPriceRepo
from prime_jennie_runtime.kis_gateway.price_scheduler_app import _fetch_and_upsert_daily

logger = logging.getLogger("backfill_daily_prices")


_UNIVERSE_SQL = (
    "SELECT stock_code FROM stock_masters "
    "WHERE is_active = TRUE AND market_cap IS NOT NULL "
    "ORDER BY market_cap DESC LIMIT $1"
)


def dsn_from_env() -> str:
    """seed_scheduled_jobs 와 동일 패턴. pj_runtime 으로 INSERT/UPDATE."""
    user = os.environ.get("POSTGRES_USER", "pj_runtime")
    password = os.environ.get("POSTGRES_PASSWORD", "dev_password")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "prime_jennie_v3")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def resolve_gateway_url(cli_value: str | None) -> str:
    """CLI > env > docker 기본 (`kis-gateway:8080`)."""
    if cli_value:
        return cli_value.rstrip("/")
    return os.environ.get("KIS_GATEWAY_URL", "http://kis-gateway:8080").rstrip("/")


async def fetch_universe(pool: asyncpg.Pool, limit: int) -> list[str]:
    records = await pool.fetch(_UNIVERSE_SQL, limit)
    return [r["stock_code"] for r in records]


async def _call_with_retry(
    http: httpx.AsyncClient,
    repo: PostgresPriceRepo,
    gateway_url: str,
    ticker: str,
    days: int,
    max_attempts: int,
    backoff: float,
) -> int:
    """지수 backoff 로 재시도. `_fetch_and_upsert_daily` 의 얇은 래퍼."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _fetch_and_upsert_daily(http, repo, gateway_url, ticker, days)
        except Exception as e:  # httpx.HTTPError / RuntimeError / KISApiError via 502
            if attempt >= max_attempts:
                raise
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(
                "retry %d/%d ticker=%s after %.2fs: %s",
                attempt,
                max_attempts,
                ticker,
                wait,
                e,
            )
            await asyncio.sleep(wait)


async def backfill(
    *,
    dsn: str,
    gateway_url: str,
    limit: int,
    days: int,
    sleep_sec: float,
    max_attempts: int,
    backoff: float,
    dry_run: bool,
) -> int:
    """리턴값: exit code (0 = 모두 성공, 1 = 일부 실패, 2 = universe 비어있음)."""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        tickers = await fetch_universe(pool, limit)
        if not tickers:
            logger.error(
                "universe 비어있음 — stock_masters 에 market_cap NOT NULL row 가 없음. "
                "refresh_market_caps job 을 먼저 돌려야 함."
            )
            return 2
        logger.info(
            "backfill 대상: %d 종목 × %d 일 (sleep=%.2fs gateway=%s)",
            len(tickers),
            days,
            sleep_sec,
            gateway_url,
        )
        if dry_run:
            for t in tickers[:10]:
                logger.info("[dry-run] would fetch %s days=%d", t, days)
            if len(tickers) > 10:
                logger.info("[dry-run] ... +%d 종목 더", len(tickers) - 10)
            return 0

        repo = PostgresPriceRepo(conn=pool)
        ok_tickers = 0
        ok_rows = 0
        errors: list[tuple[str, str]] = []
        async with httpx.AsyncClient() as http:
            for idx, ticker in enumerate(tickers, start=1):
                try:
                    rows = await _call_with_retry(
                        http,
                        repo,
                        gateway_url,
                        ticker,
                        days,
                        max_attempts=max_attempts,
                        backoff=backoff,
                    )
                    ok_tickers += 1
                    ok_rows += rows
                    if idx % 20 == 0 or idx == len(tickers):
                        logger.info(
                            "progress %d/%d ok_tickers=%d ok_rows=%d errors=%d",
                            idx,
                            len(tickers),
                            ok_tickers,
                            ok_rows,
                            len(errors),
                        )
                except Exception as e:
                    errors.append((ticker, f"{type(e).__name__}: {e}"))
                    logger.warning("final fail ticker=%s: %s", ticker, e)
                # 종목간 간격 — 실패해도 rate limit 유지.
                if idx < len(tickers) and sleep_sec > 0:
                    await asyncio.sleep(sleep_sec)

        logger.info(
            "backfill 완료: universe=%d ok_tickers=%d ok_rows=%d errors=%d",
            len(tickers),
            ok_tickers,
            ok_rows,
            len(errors),
        )
        if errors:
            logger.warning("실패 종목 %d 개:", len(errors))
            for tkr, detail in errors[:20]:
                logger.warning("  %s — %s", tkr, detail)
            if len(errors) > 20:
                logger.warning("  ... +%d 개 더", len(errors) - 20)
            return 1
        return 0
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="daily_prices 일회성 backfill (KIS)")
    p.add_argument("--limit", type=int, default=200, help="universe 종목 수 (default 200)")
    p.add_argument("--days", type=int, default=60, help="종목당 조회 일 수 (default 60)")
    p.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="종목간 간격. paper=0.5 (2/sec), 실계좌=0.06 (19/sec) 권장",
    )
    p.add_argument(
        "--gateway-url",
        default=None,
        help="KIS Gateway base URL. 기본은 env KIS_GATEWAY_URL 또는 http://kis-gateway:8080",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="종목별 재시도 횟수 (default 3, 지수 backoff)",
    )
    p.add_argument(
        "--backoff",
        type=float,
        default=1.0,
        help="재시도 초기 대기 (default 1.0s, 이후 ×2)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="universe 만 출력하고 실 호출 생략",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    return asyncio.run(
        backfill(
            dsn=dsn_from_env(),
            gateway_url=resolve_gateway_url(args.gateway_url),
            limit=args.limit,
            days=args.days,
            sleep_sec=args.sleep,
            max_attempts=args.max_attempts,
            backoff=args.backoff,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
