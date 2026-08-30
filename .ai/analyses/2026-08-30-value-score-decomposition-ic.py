"""가치 점수 세 부품의 예측력(IC) — 읽기 전용. 잡의 pooled Spearman 방식과 동일."""

from __future__ import annotations

import asyncio
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from prime_jennie_runtime.infra.config import PostgresConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.slow_loop.scout import quant
from prime_jennie_runtime.slow_loop.scout.deterministic_scout import (
    _compute_sector_pctiles,
    _effective_per,
)
from prime_jennie_runtime.slow_loop.scout.enrichment import enrich_universe
from prime_jennie_runtime.slow_loop.scout.feeders.real import RealUniverseFeeder

START, END = date(2026, 6, 1), date(2026, 8, 14)
HORIZONS = (5, 10)


def per_part(c):
    if c.sector_per_pctile is not None:
        return quant._pctile_to_score_10(c.sector_per_pctile)
    eper = _effective_per(c)
    if eper is None:
        return None
    for lim, s in ((8, 10.0), (12, 7.0), (15, 5.5), (20, 4.0), (30, 2.5), (50, 2.0)):
        if eper < lim:
            return s
    return 1.5


def pbr_part(c):
    ft = c.financial_trend
    if c.sector_pbr_pctile is not None:
        return quant._pctile_to_score_5(c.sector_pbr_pctile, floor=1.0)
    if ft and ft.pbr is not None and ft.pbr > 0:
        for lim, s in ((0.7, 5.0), (1.0, 4.0), (1.5, 2.5), (3.0, 1.5)):
            if ft.pbr < lim:
                return s
        return 1.0
    return None


def high_part(c):
    snap = c.snapshot
    if not (snap and snap.high_52w and snap.price):
        return None
    dd = (snap.price / snap.high_52w - 1) * 100
    if dd < -30:
        return 1.5
    if dd < -15:
        return 3.5
    if dd < -5:
        return 4.0
    return 5.0


async def main() -> None:
    engine = create_engine(PostgresConfig())

    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT DISTINCT price_date FROM daily_prices "
                "WHERE price_date BETWEEN :a AND :b ORDER BY price_date"
            ),
            {"a": START, "b": END},
        )
        dates = [r[0] for r in res.all()]
        res = await conn.execute(
            text(
                "SELECT stock_code, price_date, close_price FROM daily_prices "
                "WHERE price_date >= :a ORDER BY stock_code, price_date"
            ),
            {"a": START},
        )
        prices: dict[str, list[tuple]] = {}
        for code, d, cp in res.all():
            prices.setdefault(code, []).append((d, float(cp)))

    print(f"거래일 {len(dates)}개 ({dates[0]} ~ {dates[-1]})")

    rows = []
    for i, d in enumerate(dates):
        universe = await RealUniverseFeeder(engine).fetch(d)
        enriched = await enrich_universe(engine, universe, as_of=d)
        _compute_sector_pctiles(enriched)
        for code, c in enriched.items():
            pp, bp, hp = per_part(c), pbr_part(c), high_part(c)
            series = prices.get(code, [])
            idx = next((k for k, (pd_, _) in enumerate(series) if pd_ >= d), None)
            if idx is None:
                continue
            entry = series[idx][1]
            if entry <= 0:
                continue
            mom = quant._momentum_score(c.daily_prices, None) if c.daily_prices else None
            row = {
                "date": d,
                "code": code,
                "per": pp,
                "pbr": bp,
                "high52": hp,
                "value": quant._value_score(c),
                "per_pbr": (pp + bp) if (pp is not None and bp is not None) else None,
                "per_pbr_hi": (pp + bp + hp) if None not in (pp, bp, hp) else None,
                "momentum": mom,
            }
            ok = False
            for h in HORIZONS:
                j = idx + h
                if j < len(series):
                    row[f"fwd_{h}"] = (series[j][1] - entry) / entry * 100
                    ok = True
            if ok:
                rows.append(row)
        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{len(dates)} 처리")

    df = pd.DataFrame(rows)
    print(f"\n표본 {len(df)} 행 (종목×날짜)")

    def report(sub, label):
        print(f"\n=== {label} (n={len(sub)}) ===")
        print(f"{'부품':10s} " + "  ".join(f"T+{h}" for h in HORIZONS))
        for fn in ("per", "pbr", "high52", "per_pbr", "per_pbr_hi", "value", "momentum"):
            cells = []
            for h in HORIZONS:
                col = f"fwd_{h}"
                s = sub[sub[col].notna() & sub[fn].notna()]
                c = s[fn].corr(s[col], method="spearman") if len(s) > 50 else np.nan
                cells.append(f"{c:+.3f}" if not np.isnan(c) else "  n/a")
            print(f"{fn:10s} " + "  ".join(cells))

    print("\n=== 52주고점 부품 vs 모멘텀 팩터 상관 ===")
    sub = df[df["high52"].notna() & df["momentum"].notna()]
    print(
        f"  spearman {sub['high52'].corr(sub['momentum'], method='spearman'):+.3f} (n={len(sub)})"
    )

    report(df, f"전 구간 {START} ~ {END}")
    late = df[df["date"] >= date(2026, 8, 1)]
    report(late, "8월 구간만 (FnGuide 수리 이후)")

    await engine.dispose()


asyncio.run(main())
