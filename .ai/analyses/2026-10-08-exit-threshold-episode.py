import sys

import numpy as np
import pandas as pd

df = pd.read_csv(sys.argv[1], parse_dates=["gd"])
df["alpha"] = df["pnl_pct"] - (df["b1"] / df["b0"] - 1) * 100
df["carry"] = df["conv"] < 0.66
days = sorted(df.gd.unique())
idx = {d: i for i, d in enumerate(days)}
df = df.sort_values(["ticker", "gd"])
df["di"] = df.gd.map(idx)
df["ep"] = (df.groupby("ticker").di.diff().fillna(99) > 1).groupby(df.ticker).cumsum()
df["pos"] = df.groupby(["ticker", "ep"]).cumcount() + 1  # 에피소드 안 몇 번째 발행
df["posb"] = pd.cut(df.pos, [0, 1, 3, 6, 100], labels=["1", "2-3", "4-6", "7+"])


def s(x):
    return (
        f"{x.mean():+.2f} (se {x.std(ddof=1) / np.sqrt(len(x)):.2f}, n={len(x)})"
        if len(x) > 1
        else f"{x.mean():+.2f} (n=1)"
    )


print("에피소드 안 순번별")
for k, g in df.groupby("posb", observed=True):
    print("  ", k, s(g.alpha))
print("\n순번 × 상태")
for (p, c), g in df.groupby(["posb", "carry"], observed=True):
    print(f"   순번 {p:4s} {'이월분' if c else '진입권'}", s(g.alpha))
print("\n진입권만, 순번 × 점수 구간")
e = df[~df.carry].copy()
e["cb"] = pd.cut(e.conv, [0.66, 0.70, 0.75, 1.0], right=False)
for (p, c), g in e.groupby(["posb", "cb"], observed=True):
    print(f"   순번 {p:4s} {str(c):14s}", s(g.alpha))
print("\n청산 사유 비율 (이월분 / 진입권)")
print(
    pd.crosstab(df.exit_reason, df.carry, normalize="columns")
    .round(2)
    .rename(columns={False: "진입권", True: "이월분"})
)
