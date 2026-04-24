"""과거 position_sheets 를 `simulate_sheet` 로 재현하는 driver.

fast_loop.exit_evaluator 를 그대로 재사용하는 v3 백테스트 runner 에 운영 DB 의
실제 시트를 먹여서 "이 시트를 그때 발행했으면 어떻게 끝났을까" 를 측정한다.
scout 프롬프트/exit rule 튜닝의 초기 회귀 테스트로 쓸 수 있다.

사용 예:

    # 단일 시트
    uv run python -m scripts.backtest_replay --sheet-id ps_20260420_093000_abcd

    # 최근 N 일간 발행된 모든 시트
    uv run python -m scripts.backtest_replay --since 2026-04-20

    # 보유 기간 상한/자본 조정
    uv run python -m scripts.backtest_replay --since 2026-04-20 \\
        --lookahead-days 20 --capital 5000000

운영 DB 는 MS-01 이므로 실제 돌릴 땐 컨테이너 안에서 실행:

    docker compose exec job-worker \\
        uv run python -m scripts.backtest_replay --since 2026-04-20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from prime_jennie_runtime.backtest import (
    BacktestConfig,
    SheetBacktestResult,
    format_report,
    load_daily_bars,
    simulate_sheet,
    summarize,
)
from prime_jennie_runtime.infra.config import PostgresConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger("backtest_replay")


async def _load_sheets(
    engine,
    *,
    sheet_id: str | None,
    since: date | None,
) -> list[PositionSheet]:
    if sheet_id:
        query = text("SELECT sheet_json FROM position_sheets WHERE sheet_id = :sid")
        params = {"sid": sheet_id}
    elif since:
        query = text(
            "SELECT sheet_json FROM position_sheets "
            "WHERE generated_at >= :since ORDER BY generated_at ASC"
        )
        params = {"since": datetime.combine(since, datetime.min.time())}
    else:
        raise ValueError("either sheet_id or since is required")

    async with engine.connect() as conn:
        result = await conn.execute(query, params)
        rows = result.mappings().all()

    sheets: list[PositionSheet] = []
    for row in rows:
        raw = row["sheet_json"]
        # JSONB 가 dict 로 오지만 드물게 문자열로 오는 환경도 있어 방어적으로 처리
        data = json.loads(raw) if isinstance(raw, str) else raw
        try:
            sheets.append(PositionSheet.model_validate(data))
        except Exception as e:
            logger.warning("sheet validation failed: %s — %s", data.get("sheet_id"), e)
    return sheets


async def _backtest_one(
    engine,
    sheet: PositionSheet,
    *,
    lookback_days: int,
    lookahead_days: int,
    capital: float,
    config: BacktestConfig,
) -> SheetBacktestResult:
    gen_date = sheet.generated_at.date()
    start = gen_date - timedelta(days=lookback_days)
    end = gen_date + timedelta(days=lookahead_days)

    bars = await load_daily_bars(engine, sheet.ticker, start=start, end=end)
    if not bars:
        return SheetBacktestResult(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            strategy_tag=sheet.strategy_tag,
            filled=False,
            trades=[],
            exit_reason="no_price_data",
        )

    # entry_index: bars 중 price_date == gen_date 인 index. 없으면 그 이후 첫 영업일
    entry_index = 0
    for i, bar in enumerate(bars):
        if bar.price_date >= gen_date:
            entry_index = i
            break
    else:
        # gen_date 가 end 보다 크면 (드물다) 맨 뒤. simulate_sheet 가 처리
        entry_index = max(0, len(bars) - 1)

    return simulate_sheet(
        sheet,
        bars,
        entry_index=entry_index,
        capital_krw=capital,
        config=config,
    )


def _result_to_json_dict(r: SheetBacktestResult) -> dict:
    d = asdict(r)
    # datetime/date → ISO 문자열
    for k in ("entry_ts", "exit_ts"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for t in d.get("trades", []):
        if t.get("ts") is not None:
            t["ts"] = t["ts"].isoformat()
    return d


async def _main(args: argparse.Namespace) -> int:
    config = PostgresConfig()
    engine = create_engine(config)

    sheets = await _load_sheets(
        engine,
        sheet_id=args.sheet_id,
        since=args.since,
    )
    logger.info("loaded %d sheets", len(sheets))

    cfg = BacktestConfig(
        buy_fee_pct=args.buy_fee,
        sell_fee_pct=args.sell_fee,
        slippage_pct=args.slippage,
    )

    results: list[SheetBacktestResult] = []
    for sheet in sheets:
        r = await _backtest_one(
            engine,
            sheet,
            lookback_days=args.lookback_days,
            lookahead_days=args.lookahead_days,
            capital=args.capital,
            config=cfg,
        )
        results.append(r)
        status = r.exit_reason or ("open" if r.filled else "unfilled")
        logger.info(
            "%s %s %s pnl=%+.2f%% (%s)",
            sheet.sheet_id,
            sheet.ticker,
            sheet.strategy_tag,
            r.pnl_pct,
            status,
        )

    summary = summarize(results)
    print(format_report(summary))

    if args.output:
        out = Path(args.output)
        out.write_text(
            json.dumps(
                {
                    "summary": asdict(summary),
                    "results": [_result_to_json_dict(r) for r in results],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        logger.info("wrote %s", out)

    await engine.dispose()
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--sheet-id", help="단일 sheet_id 재현")
    group.add_argument(
        "--since",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        help="이 날짜 이후 발행된 모든 시트 (YYYY-MM-DD)",
    )
    p.add_argument("--lookback-days", type=int, default=40)
    p.add_argument("--lookahead-days", type=int, default=15)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--buy-fee", type=float, default=0.015)
    p.add_argument("--sell-fee", type=float, default=0.195)
    p.add_argument("--slippage", type=float, default=0.1)
    p.add_argument("--output", help="JSON 결과 파일 경로 (선택)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(asyncio.run(_main(args)))
