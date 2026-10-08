"""유니버스 전체 요인 IC — 국면별, 단면(종목 간)·시계열(같은 종목 평소 대비) 둘 다.

입력: 아침 첫 회차 유니버스 점수 CSV, 일봉 CSV.
수익률: 점수일 시가 매수 → h 거래일째 종가(h=1 은 당일 종가).
IC: 날짜마다 Spearman, 날짜 평균. t 는 겹침(h-1) 만큼 Newey-West 보정.
"""

import sys

import numpy as np
import pandas as pd

scores = pd.read_csv(sys.argv[1], parse_dates=["score_date"], dtype={"stock_code": str})
prices = pd.read_csv(sys.argv[2], parse_dates=["price_date"], dtype={"stock_code": str})
FACTORS = [
    "total_quant_score",
    "momentum_score",
    "technical_score",
    "sector_momentum_score",
    "supply_demand_score",
    "news_score",
    "value_score",
    "quality_score",
]
HORIZONS = [1, 3, 5, 10]
PERIODS = [
    ("3-18~5-21", "2026-03-18", "2026-05-21"),
    ("5-22~8-14 (@1)", "2026-05-22", "2026-08-14"),
    ("8-15~ (@2)", "2026-08-15", "2026-12-31"),
]

# 거래일 축과 종목별 시가·종가 표
op = prices.pivot(index="price_date", columns="stock_code", values="open_price").sort_index()
cl = prices.pivot(index="price_date", columns="stock_code", values="close_price").sort_index()
tdays = op.index
pos = {d: i for i, d in enumerate(tdays)}

scores = scores[scores.score_date.isin(pos)].copy()
for h in HORIZONS:
    fwd = {}
    for d in scores.score_date.unique():
        i = pos[d]
        if i + h - 1 >= len(tdays):
            continue
        fwd[d] = cl.iloc[i + h - 1] / op.iloc[i] - 1
    f = pd.DataFrame(fwd).T.stack().rename(f"r{h}").reset_index()
    f.columns = ["score_date", "stock_code", f"r{h}"]
    scores = scores.merge(f, on=["score_date", "stock_code"], how="left")

# 같은 종목의 직전 20 회 평균 대비 (미래 정보 없이)
scores = scores.sort_values(["stock_code", "score_date"])
for fac in FACTORS:
    past = scores.groupby("stock_code")[fac].transform(
        lambda s: s.shift(1).rolling(20, min_periods=10).mean()
    )
    scores[f"{fac}__dev"] = scores[fac] - past


def nw_t(x: np.ndarray, lag: int) -> float:
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 5:
        return float("nan")
    m = x.mean()
    e = x - m
    v = e @ e / n
    for k in range(1, lag + 1):
        w = 1 - k / (lag + 1)
        v += 2 * w * (e[k:] @ e[:-k]) / n
    return m / np.sqrt(v / n)


def daily_ic(df: pd.DataFrame, col: str, ret: str) -> np.ndarray:
    out = []
    for _, g in df.groupby("score_date"):
        g = g[[col, ret]].dropna()
        if len(g) < 30 or g[col].nunique() < 3:
            continue
        out.append(g[col].rank().corr(g[ret].rank()))
    return np.array(out)


def table(suffix: str, title: str) -> None:
    print(f"\n=== {title} ===")
    for pname, a, b in PERIODS:
        sub = scores[(scores.score_date >= a) & (scores.score_date <= b)]
        print(f"\n[{pname}]  날짜 {sub.score_date.nunique()}  행 {len(sub)}")
        print(f"  {'요인':22s}" + "".join(f"  T+{h:<2d} IC(t)    " for h in HORIZONS))
        for fac in FACTORS:
            row = f"  {fac.replace('_score', ''):22s}"
            for h in HORIZONS:
                ic = daily_ic(sub, fac + suffix, f"r{h}")
                row += f"  {ic.mean():+.3f}({nw_t(ic, h - 1):+5.1f})"
            print(row)


table("", "단면 IC — 종목 간 비교")
table("__dev", "시계열 IC — 같은 종목이 평소(직전 20 회)보다 높을 때")

# 상위 20 안에서만 (선정 종목에 가까운 집단)
print("\n=== 아침 총점 상위 20 안에서의 단면 IC (T+5) ===")
top = scores[scores.groupby("score_date").total_quant_score.rank(ascending=False) <= 20]
for pname, a, b in PERIODS:
    sub = top[(top.score_date >= a) & (top.score_date <= b)]
    row = f"  {pname:16s}"
    for fac in FACTORS:
        ic = []
        for _, g in sub.groupby("score_date"):
            g = g[[fac, "r5"]].dropna()
            if len(g) >= 10 and g[fac].nunique() >= 3:
                ic.append(g[fac].rank().corr(g.r5.rank()))
        ic = np.array(ic)
        row += f" {fac.replace('_score', '')[:6]}={ic.mean():+.2f}"
    print(row)
