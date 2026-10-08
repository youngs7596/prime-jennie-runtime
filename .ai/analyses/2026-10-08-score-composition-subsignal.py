"""기술적·모멘텀 점수의 부품을 원래 신호값으로 다시 만들어 유니버스 IC 를 본다.

신호는 점수일 전날 종가까지의 일봉으로 계산한다(아침 08:30 회차가 보는 정보와 같다).
대상 종목·날짜는 아침 첫 회차 유니버스와 같다. 수익률은 점수일 시가 → h 거래일째 종가.
"""

import sys

import numpy as np
import pandas as pd

scores = pd.read_csv(sys.argv[1], parse_dates=["score_date"], dtype={"stock_code": str})
px = pd.read_csv(sys.argv[2], parse_dates=["price_date"], dtype={"stock_code": str})
op = px.pivot(index="price_date", columns="stock_code", values="open_price").sort_index()
cl = px.pivot(index="price_date", columns="stock_code", values="close_price").sort_index()
vo = px.pivot(index="price_date", columns="stock_code", values="volume").sort_index()
tdays = cl.index
pos = {d: i for i, d in enumerate(tdays)}


def rsi(c: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn)


ma5, ma20 = cl.rolling(5).mean(), cl.rolling(20).mean()
sig_today = {
    # 기술적 부품 1: 이평선 (점수 산식 그대로 0/1.5/3/5)
    "ma_part": pd.DataFrame(
        np.select([(cl > ma5) & (ma5 > ma20), cl > ma20, cl > ma5], [5.0, 3.0, 1.5], 0.0),
        index=cl.index,
        columns=cl.columns,
    ).where(ma20.notna()),
    # 기술적 부품 2: 거래량 5일/20일
    "vol_ratio": vo.rolling(5).mean() / vo.rolling(20).mean(),
    # 모멘텀 부품
    "ret_1m": cl / cl.shift(20) - 1,
    "ret_3m": cl / cl.shift(60) - 1,
    "rsi14": rsi(cl),
    # 참고: 20일선 이격도 (과열 정도)
    "gap_ma20": cl / ma20 - 1,
}
# 점수일 아침에는 전날 종가까지만 안다 → 한 거래일 밀기
sig = {k: v.shift(1) for k, v in sig_today.items()}

scores = scores[scores.score_date.isin(pos)]
rows = []
for d, g in scores.groupby("score_date"):
    i = pos[d]
    codes = [c for c in g.stock_code if c in cl.columns]
    rec = pd.DataFrame({"stock_code": codes})
    rec["score_date"] = d
    for k, v in sig.items():
        rec[k] = v.loc[d, codes].values
    for h in (1, 5, 10):
        if i + h - 1 < len(tdays):
            rec[f"r{h}"] = (cl.iloc[i + h - 1][codes] / op.iloc[i][codes] - 1).values
    rows.append(rec)
df = pd.concat(rows, ignore_index=True)

PERIODS = [
    ("4-07~5-21", "2026-04-07", "2026-05-21"),  # 60 일 수익률이 계산되는 첫 시점부터
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


print(f"{'신호':10s}" + "".join(f"  {p:>22s}" for p, _, _ in PERIODS))
for k in sig:
    for h in (1, 5, 10):
        line = f"{k:10s} T+{h:<2d}"
        for _, a, b in PERIODS:
            sub = df[(df.score_date >= a) & (df.score_date <= b)]
            ics = []
            for _, g in sub.groupby("score_date"):
                g = g[[k, f"r{h}"]].dropna()
                if len(g) >= 30 and g[k].nunique() >= 3:
                    ics.append(g[k].rank().corr(g[f"r{h}"].rank()))
            ics = np.array(ics)
            line += f"   {ics.mean():+.3f} (t{nw_t(ics, h - 1):+5.1f}, d{len(ics):3d})"
        print(line)
    print()

# 거래량 급증을 점수 산식 구간으로 나눠 평균 초과수익(T+5, 날짜별 단면 평균 대비)
df["ex5"] = df.r5 - df.groupby("score_date").r5.transform("mean")
df["ex1"] = df.r1 - df.groupby("score_date").r1.transform("mean")
bins = [0, 1.0, 1.2, 1.5, 2.0, 99]
df["vb"] = pd.cut(
    df.vol_ratio, bins, labels=["≤1.0(0.5점)", "1.0~1.2(2)", "1.2~1.5(3)", "1.5~2.0(4)", ">2.0(5)"]
)
print("거래량 5일/20일 구간별 평균 초과수익(%) — 전 기간")
print(
    (df.groupby("vb", observed=True)[["ex1", "ex5"]].mean() * 100)
    .round(2)
    .assign(n=df.groupby("vb", observed=True).size())
)
print("\n이평선 부품 점수별 평균 초과수익(%) — 전 기간")
print(
    (df.groupby("ma_part")[["ex1", "ex5"]].mean() * 100)
    .round(2)
    .assign(n=df.groupby("ma_part").size())
)
df["gb"] = pd.qcut(df.gap_ma20, 5, labels=["하위20%", "2", "3", "4", "상위20%"])
print("\n20일선 이격도 5분위별 평균 초과수익(%) — 전 기간")
print((df.groupby("gb", observed=True)[["ex1", "ex5"]].mean() * 100).round(2))
