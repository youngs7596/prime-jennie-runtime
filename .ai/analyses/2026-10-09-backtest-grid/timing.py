"""진입·청산 시각 비교. 점수일 d 의 유니버스 전 종목, N 거래일 보유. 비용 0.41 차감, KODEX 같은 시각 기준 대비."""

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
X = pd.read_csv(f"{D}/factor_panel.csv", dtype={"stock_code": str})
dates = list(C.index)
di = {d: i for i, d in enumerate(dates)}
X = X[X.score_date.isin(di) & X.stock_code.isin(set(C.columns))]


def P(d):
    return (
        "P0"
        if d < "2026-05-01"
        else "P1"
        if d < "2026-06-30"
        else "P2"
        if d < "2026-08-15"
        else "P3"
    )


X["P"] = X.score_date.map(P)
ii = X.score_date.map(di).to_numpy()
cc = X.stock_code.to_numpy()
cols = {c: j for j, c in enumerate(C.columns)}
jj = np.array([cols[c] for c in cc])
kj = cols["069500"]
Cv, Ov = C.to_numpy(), O.to_numpy()


def px_at(kind, i, j):
    ok = i < len(dates)
    out = np.full(len(i), np.nan)
    out[ok] = (Cv if kind == "C" else Ov)[i[ok], j[ok]]
    return out


res = []
for N in (1, 3, 5):
    for ent, eoff, ext, xoff in [
        ("시가", 0, "종가", N),
        ("종가", 0, "종가", N),
        ("종가", 0, "시가", N),
        ("시가", 1, "시가", N + 1),
        ("시가", 0, "시가", N),
    ]:
        # 진입: d 시가(장 시작 직후 매수 ≈ 실제 fast loop) 또는 d 종가(paper 측정)
        e = px_at("O" if ent == "시가" else "C", ii + eoff, jj)
        x = px_at("O" if ext == "시가" else "C", ii + xoff, jj)
        ek = px_at("O" if ent == "시가" else "C", ii + eoff, np.full(len(ii), kj))
        xk = px_at("O" if ext == "시가" else "C", ii + xoff, np.full(len(ii), kj))
        a = (x / e - 1) * 100 - (xk / ek - 1) * 100 - 0.41
        pnl = (x / e - 1) * 100 - 0.41
        df = pd.DataFrame({"P": X.P, "d": X.score_date, "a": a, "pnl": pnl}).dropna()
        day = df.groupby(["P", "d"])[["a", "pnl"]].mean().groupby("P").mean()
        lab = f"{N}일: d{'+' + str(eoff) if eoff else ''} {ent} → d+{xoff} {ext}"
        res.append(
            {
                "방식": lab,
                **{f"{p} 손익": round(v, 2) for p, v in day.pnl.items()},
                **{f"{p} 알파": round(v, 2) for p, v in day.a.items()},
            }
        )
print(pd.DataFrame(res).to_string(index=False))
