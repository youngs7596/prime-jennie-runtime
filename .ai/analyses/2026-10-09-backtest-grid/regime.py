import sys

import numpy as np
import pandas as pd

D = sys.argv[1]
px = pd.read_csv(f"{D}/ohlcv_all.csv", dtype={"stock_code": str})
C = (
    px.pivot(index="price_date", columns="stock_code", values="close_price")
    .sort_index()
    .astype(float)
)
O = (
    px.pivot(index="price_date", columns="stock_code", values="open_price")
    .sort_index()
    .astype(float)
)
uni = pd.concat(
    [
        pd.read_csv(f"{D}/{f}", dtype={"stock_code": str}, usecols=["stock_code"])
        for f in ["gscores_mar.csv", "gscores_early.csv", "gscores.csv"]
    ]
).stock_code.unique()
U = [c for c in uni if c in C and c != "069500"]
C = C[C.index >= "2026-03-10"]
O = O.loc[C.index]
r = C.pct_change(fill_method=None) * 100
ew = r[U].mean(axis=1)
k = r["069500"]
spread = (ew - k).dropna()
print(
    f"거래일 {len(spread)}  동일가중−KODEX 일평균 {spread.mean():+.3f}%p, 표준편차 {spread.std():.2f}"
)
print(
    "자기상관 (어제 쏠림이 오늘도 이어지나):",
    {l: round(spread.autocorr(l), 3) for l in (1, 2, 3, 5)},
)
# 지난 n일 누적 차이 → 다음 m일 누적 차이
for n, m in [(1, 1), (5, 1), (5, 5), (20, 5), (20, 20)]:
    past = spread.rolling(n).sum()
    fut = spread[::-1].rolling(m).sum()[::-1].shift(-1)
    x = pd.concat([past, fut], axis=1).dropna()
    hit = (np.sign(x.iloc[:, 0]) == np.sign(x.iloc[:, 1])).mean()
    print(
        f"  지난 {n:2d}일 → 다음 {m:2d}일: 상관 {x.corr().iloc[0, 1]:+.2f}, 방향 일치 {hit * 100:.0f}% (n={len(x)})"
    )
# 전략: 지난 20일 동일가중이 이겼으면 다음날 동일가중, 아니면 KODEX
for n in (5, 10, 20):
    sig = (spread.rolling(n).sum() > 0).shift(1).dropna()
    idx = sig.index
    s = pd.Series(np.where(sig.astype(bool), ew[idx], k[idx]), index=idx)
    print(
        f"  전환 전략({n}일): 누적 {((1 + s / 100).prod() - 1) * 100:+.1f}%  vs KODEX {((1 + k[s.index] / 100).prod() - 1) * 100:+.1f}%  동일가중 {((1 + ew[s.index] / 100).prod() - 1) * 100:+.1f}%  (전환 {int((sig.astype(float).diff().abs() > 0).sum())}회)"
    )
# 밤사이 vs 장중
on = (O / C.shift(1) - 1) * 100
idr = (C / O - 1) * 100
print("\n밤사이(전일 종가→시가) / 장중(시가→종가) 일평균 %:")
print(f"  KODEX200   밤 {on['069500'].mean():+.3f}  장중 {idr['069500'].mean():+.3f}")
print(f"  동일가중   밤 {on[U].mean(axis=1).mean():+.3f}  장중 {idr[U].mean(axis=1).mean():+.3f}")
for a, b in [
    ("2026-03-10", "2026-05-01"),
    ("2026-05-01", "2026-06-30"),
    ("2026-06-30", "2026-08-15"),
    ("2026-08-15", "2026-10-09"),
]:
    s = slice(a, b)
    print(
        f"  {a}~ 동일가중 밤 {on[U].loc[s].mean(axis=1).mean():+.3f} 장중 {idr[U].loc[s].mean(axis=1).mean():+.3f} | KODEX 밤 {on['069500'].loc[s].mean():+.3f} 장중 {idr['069500'].loc[s].mean():+.3f}"
    )
