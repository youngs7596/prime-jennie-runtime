"""요인 점검: 기간 네 개 × 요인별 일별 순위상관(IC)과 상위 1/5 초과수익. 점수일 d 아침에 알 수 있는
정보(전날 종가까지)로 요인을 만들고, d 종가 진입 → d+h 종가. 초과 = 종목 − 같은 날 유니버스 평균."""

import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

D = sys.argv[1]
pd.set_option("display.width", 250)
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
V = px.pivot(index="price_date", columns="stock_code", values="volume").sort_index().astype(float)
dates = list(C.index)
di = {d: i for i, d in enumerate(dates)}
sc = pd.concat(
    [
        pd.read_csv(f"{D}/{f}", dtype={"stock_code": str})
        for f in ["gscores_mar.csv", "gscores_early.csv", "gscores.csv"]
    ]
)
sc = sc[sc.score_date.isin(di)].drop_duplicates(["score_date", "stock_code"])


def period(d):
    if d < "2026-05-01":
        return "P0 3~4월"
    if d < "2026-06-30":
        return "P1 5~6월"
    if d < "2026-08-15":
        return "P2 7~8월중"
    return "P3 8월중~10월"


sc["P"] = sc.score_date.map(period)
# 가격 요인 (전날 종가까지)
ret = C.pct_change()
feat = {
    "r1": ret,
    "r5": C.pct_change(5),
    "r20": C.pct_change(20),
    "vol20": ret.rolling(20).std(),
    "turn20": (C * V).rolling(20).mean(),
    "ma20gap": C / C.rolling(20).mean() - 1,
}


def lag(df, d):  # d 전날 값
    i = di[d]
    return df.iloc[i - 1] if i >= 1 else None


rows = []
for d, g in sc.groupby("score_date"):
    i = di[d]
    for f, df in feat.items():
        g[f] = g.stock_code.map(df.iloc[i - 1]) if i >= 1 else np.nan
    for h in (1, 5):
        if i + h < len(dates):
            g[f"fw{h}"] = g.stock_code.map(C.iloc[i + h] / C.iloc[i] - 1) * 100
    # 밤사이/장중 (d 종가 → d+1 시가, d+1 시가 → d+1 종가)
    if i + 1 < len(dates):
        g["on"] = g.stock_code.map(O.iloc[i + 1] / C.iloc[i] - 1) * 100
        g["id"] = g.stock_code.map(C.iloc[i + 1] / O.iloc[i + 1] - 1) * 100
    rows.append(g)
X = pd.concat(rows)
X.to_csv(f"{D}/factor_panel.csv", index=False)
FS = [
    "total",
    "qual",
    "val",
    "sd",
    "sec",
    "news",
    "tech",
    "mom",
    "r1",
    "r5",
    "r20",
    "vol20",
    "turn20",
    "ma20gap",
]
for h in (1, 5):
    col = f"fw{h}"
    out = {}
    for P, gp in X.groupby("P"):
        res = {}
        for f in FS:
            ics, qs = [], []
            for d, g in gp.groupby("score_date"):
                g = g.dropna(subset=[f, col])
                if len(g) < 30 or g[f].nunique() < 5:
                    continue
                ics.append(spearmanr(g[f], g[col]).correlation)
                q = g[f].rank(pct=True)
                qs.append(g[col][q > 0.8].mean() - g[col].mean())
            if len(ics) < 5:
                res[f] = ""
                continue
            ics = np.array(ics)
            res[f] = (
                f"{ics.mean():+.3f}({ics.mean() / ics.std() * len(ics) ** 0.5:+.1f}) {np.mean(qs):+.2f}"
            )
        out[f"{P} n={gp.score_date.nunique()}"] = res
    print(f"\n=== T+{h}: IC 평균(t) 상위20% 초과수익%p")
    print(pd.DataFrame(out).to_string())
