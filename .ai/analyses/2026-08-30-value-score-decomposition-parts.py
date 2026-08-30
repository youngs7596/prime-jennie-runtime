"""가치 점수(20점) 분해 — 읽기 전용. 컨테이너 안에서 실행."""

from __future__ import annotations

import asyncio
import statistics
from datetime import date

from prime_jennie_runtime.infra.config import PostgresConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.slow_loop.scout import quant
from prime_jennie_runtime.slow_loop.scout.deterministic_scout import (
    _compute_sector_pctiles,
    _effective_per,
)
from prime_jennie_runtime.slow_loop.scout.enrichment import _load_fundamentals, enrich_universe
from prime_jennie_runtime.slow_loop.scout.feeders.real import RealUniverseFeeder

AS_OF = date(2026, 8, 28)
STALE = date(2026, 8, 22)  # PER/PBR 갱신(8-24) 직전


def per_part(c) -> float:
    if c.sector_per_pctile is not None:
        return quant._pctile_to_score_10(c.sector_per_pctile)
    eper = _effective_per(c)
    if eper is None:
        return 0.0
    for lim, s in ((8, 10.0), (12, 7.0), (15, 5.5), (20, 4.0), (30, 2.5), (50, 2.0)):
        if eper < lim:
            return s
    return 1.5


def pbr_part(c) -> float:
    ft = c.financial_trend
    if c.sector_pbr_pctile is not None:
        return quant._pctile_to_score_5(c.sector_pbr_pctile, floor=1.0)
    if ft and ft.pbr is not None and ft.pbr > 0:
        for lim, s in ((0.7, 5.0), (1.0, 4.0), (1.5, 2.5), (3.0, 1.5)):
            if ft.pbr < lim:
                return s
        return 1.0
    return 0.0


def high_part(c) -> float:
    snap = c.snapshot
    if not (snap and snap.high_52w and snap.price):
        return 0.0
    dd = (snap.price / snap.high_52w - 1) * 100
    if dd < -30:
        return 1.5
    if dd < -15:
        return 3.5
    if dd < -5:
        return 4.0
    return 5.0


def stats(name, xs):
    xs = list(xs)
    if not xs:
        print(f"  {name}: (없음)")
        return
    print(
        f"  {name:14s} n={len(xs):3d} 평균 {statistics.mean(xs):5.2f} "
        f"표준편차 {statistics.pstdev(xs):4.2f} 최소 {min(xs):4.1f} 최대 {max(xs):4.1f}"
    )


async def main() -> None:
    engine = create_engine(PostgresConfig())
    universe = await RealUniverseFeeder(engine).fetch(AS_OF)
    enriched = await enrich_universe(engine, universe, as_of=AS_OF)
    _compute_sector_pctiles(enriched)
    cands = list(enriched.values())

    print(f"\n=== 유니버스 {len(universe)}종목 / 보강 {len(cands)}종목 (as_of {AS_OF}) ===")
    have_ft = [c for c in cands if c.financial_trend]
    fwd = [
        c for c in cands if c.consensus and c.consensus.forward_per and c.consensus.forward_per > 0
    ]
    trail = [c for c in have_ft if c.financial_trend.per and c.financial_trend.per > 0]
    print(
        f"재무행 있음 {len(have_ft)} / forward PER 있음 {len(fwd)} / trailing PER 있음 {len(trail)}"
    )
    print(f"섹터 PER 백분위 채워짐 {sum(1 for c in cands if c.sector_per_pctile is not None)}")
    print(f"섹터 PBR 백분위 채워짐 {sum(1 for c in cands if c.sector_pbr_pctile is not None)}")

    src_fwd = sum(
        1
        for c in have_ft
        if c.consensus and c.consensus.forward_per and c.consensus.forward_per > 0
    )
    print(f"유효 PER 출처: forward {src_fwd} / trailing {len(have_ft) - src_fwd}")

    vals = [quant._value_score(c) for c in cands]
    print("\n=== 가치 점수 20점 분해 (전 유니버스) ===")
    stats("가치 총점", vals)
    stats("PER(0-10)", (per_part(c) for c in have_ft))
    stats("PBR(0-5)", (pbr_part(c) for c in have_ft))
    stats("52주고점(0-5)", (high_part(c) for c in have_ft))
    neutral = [c for c in cands if not c.financial_trend]
    print(f"  재무행 없어 중립 10.0 처리: {len(neutral)}종목")

    # 종목 간 차이를 무엇이 만드는가 — 각 부분과 총점의 상관
    import math

    def corr(xs, ys):
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
        dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
        dy = math.sqrt(sum((b - my) ** 2 for b in ys))
        return num / (dx * dy) if dx and dy else float("nan")

    tot = [per_part(c) + pbr_part(c) + high_part(c) for c in have_ft]
    print("\n  총점과의 상관 (종목 간 차이를 누가 만드나)")
    for nm, f in (("PER", per_part), ("PBR", pbr_part), ("52주고점", high_part)):
        print(f"    {nm:9s} {corr([f(c) for c in have_ft], tot):+.3f}")

    # 8-24 갱신 전 PER/PBR 로 되돌려 재계산
    codes = list(enriched.keys())
    old_ft = await _load_fundamentals(engine, codes, as_of=STALE)
    changed_per = changed_pbr = 0
    for code, c in enriched.items():
        o = old_ft.get(code)
        n = c.financial_trend
        if o and n:
            if o.per != n.per:
                changed_per += 1
            if o.pbr != n.pbr:
                changed_pbr += 1
        c.financial_trend = o
    for c in enriched.values():
        c.sector_per_pctile = None
        c.sector_pbr_pctile = None
    _compute_sector_pctiles(enriched)
    old_vals = [quant._value_score(c) for c in enriched.values()]
    print("\n=== 8-22(갱신 전) PER/PBR 로 되돌리면 ===")
    print(f"PER 값이 바뀐 종목 {changed_per} / PBR {changed_pbr}")
    stats("가치 총점(옛)", old_vals)
    stats("가치 총점(현)", vals)
    diffs = [n - o for n, o in zip(vals, old_vals, strict=True)]
    moved = [d for d in diffs if abs(d) > 1e-9]
    print(f"  종목별 변화: 움직인 종목 {len(moved)}, 평균 변화 {statistics.mean(diffs):+.3f}")
    if moved:
        print(f"  움직인 것들의 평균 절대변화 {statistics.mean([abs(d) for d in moved]):.2f}")

    await engine.dispose()


asyncio.run(main())
