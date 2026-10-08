"""저장된 요인 점수로 가중치를 바꿔 다시 합산 → 상위 20 의 다음 성과를 국면별로 비교.

요인 점수는 이미 배점(0~20, 0~10)이 들어간 값이라, 요인을 빼거나 배수를 곱해 합산한다.
상위 20 은 날마다 새로 뽑고(이월·히스테리시스 없음), 수익률은 점수일 시가 → h 거래일째
종가, 그날 유니버스 평균 대비 초과수익. 같은 날 겹침이 있어 t 는 Newey-West(h-1).
"""

import sys

import numpy as np
import pandas as pd

scores = pd.read_csv(sys.argv[1], parse_dates=["score_date"], dtype={"stock_code": str})
px = pd.read_csv(sys.argv[2], parse_dates=["price_date"], dtype={"stock_code": str})
op = px.pivot(index="price_date", columns="stock_code", values="open_price").sort_index()
cl = px.pivot(index="price_date", columns="stock_code", values="close_price").sort_index()
tdays = cl.index
pos = {d: i for i, d in enumerate(tdays)}
scores = scores[scores.score_date.isin(pos)].copy()
F = {
    "mom": "momentum_score",
    "tech": "technical_score",
    "sect": "sector_momentum_score",
    "sd": "supply_demand_score",
    "news": "news_score",
    "val": "value_score",
    "qual": "quality_score",
}
scores["sector_momentum_score"] = scores["sector_momentum_score"].fillna(5.0)

for h in (1, 5, 10):
    vals = []
    for d, g in scores.groupby("score_date"):
        i = pos[d]
        if i + h - 1 >= len(tdays):
            vals.append(pd.Series(np.nan, index=g.index))
            continue
        r = cl.iloc[i + h - 1] / op.iloc[i] - 1
        rr = g.stock_code.map(r)
        vals.append(rr - rr.mean())
    scores[f"ex{h}"] = pd.concat(vals) * 100

VARIANTS = {
    "현행": dict(mom=1, tech=1, sect=1, sd=1, news=1, val=1, qual=1),
    "기술적 뺌": dict(mom=1, tech=0, sect=1, sd=1, news=1, val=1, qual=1),
    "기술적·모멘텀 뺌": dict(mom=0, tech=0, sect=1, sd=1, news=1, val=1, qual=1),
    "기술적·모멘텀·섹터 뺌": dict(mom=0, tech=0, sect=0, sd=1, news=1, val=1, qual=1),
    "가치·품질만": dict(mom=0, tech=0, sect=0, sd=0, news=0, val=1, qual=1),
    "기술적 거꾸로": dict(mom=1, tech=-1, sect=1, sd=1, news=1, val=1, qual=1),
}
PERIODS = [
    ("3-18~5-21", "2026-03-18", "2026-05-21"),
    ("5-22~8-14", "2026-05-22", "2026-08-14"),
    ("8-15~10-08", "2026-08-15", "2026-12-31"),
]


def nw_t(x: np.ndarray, lag: int) -> float:
    x = x[~np.isnan(x)]
    n = len(x)
    e = x - x.mean()
    v = e @ e / n
    for k in range(1, lag + 1):
        v += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return x.mean() / np.sqrt(v / n)


for h in (5, 10):
    print(f"\n상위 20 의 T+{h} 평균 초과수익 %p (t, 날짜 수) — 날마다 새로 뽑음")
    print(f"{'안':22s}" + "".join(f"{p:>24s}" for p, _, _ in PERIODS))
    for name, w in VARIANTS.items():
        s = sum(w[k] * scores[c] for k, c in F.items())
        scores["_s"] = s
        line = f"{name:22s}"
        for _, a, b in PERIODS:
            sub = scores[(scores.score_date >= a) & (scores.score_date <= b)]
            daily = []
            for _, g in sub.groupby("score_date"):
                top = g.nlargest(20, "_s")[f"ex{h}"].dropna()
                if len(top) >= 10:
                    daily.append(top.mean())
            daily = np.array(daily)
            line += f"   {daily.mean():+6.2f} (t{nw_t(daily, h - 1):+5.1f}, {len(daily):3d})"
        print(line)
