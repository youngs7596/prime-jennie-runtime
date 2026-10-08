"""이월분 역전 종목 단위 재확인 — 같은 종목 겹침·같은 날 쏠림을 걷어 낸다."""

import sys

import numpy as np
import pandas as pd
from scipy import stats

df = pd.read_csv(sys.argv[1], parse_dates=["gd", "entry_date", "exit_date"])
df["alpha"] = df["pnl_pct"] - (df["b1"] / df["b0"] - 1) * 100
df["carry"] = df["conv"] < 0.66
df["bucket"] = pd.cut(df["conv"], [0.59, 0.62, 0.66, 0.70, 0.75, 1.0], right=False)
print(f"시트 {len(df)}  종목 {df.ticker.nunique()}  발행일 {df.gd.nunique()}")
print(f"이월분 시트 {df.carry.sum()}  진입권 시트 {(~df.carry).sum()}\n")


def summ(x: pd.Series) -> str:
    se = x.std(ddof=1) / np.sqrt(len(x)) if len(x) > 1 else float("nan")
    return f"평균 {x.mean():+.2f}  표준오차 {se:.2f}  n={len(x)}"


# 0) 시트 단위 (어제 결과 재현)
print("[0] 시트 단위")
for k, g in df.groupby("carry"):
    print("  ", "이월분" if k else "진입권", summ(g.alpha))

# 1) 종목당 한 표 — 상태(이월/진입)별로 종목 평균을 낸 뒤 종목을 한 표로
print("\n[1] 종목당 한 표 (종목·상태별 평균)")
t = df.groupby(["ticker", "carry"]).alpha.mean().reset_index()
for k, g in t.groupby("carry"):
    print("  ", "이월분" if k else "진입권", summ(g.alpha))
a, b = t[t.carry].alpha, t[~t.carry].alpha
print("   두 집단 차이 t검정(웰치) p =", round(stats.ttest_ind(a, b, equal_var=False).pvalue, 4))
print("   순위 검정(맨-휘트니) p =", round(stats.mannwhitneyu(a, b).pvalue, 4))

# 1b) 구간별 종목당 한 표
print("\n[1b] 구간별 종목당 한 표")
tb = df.groupby(["bucket", "ticker"], observed=True).alpha.mean().reset_index()
for k, g in tb.groupby("bucket", observed=True):
    print(f"   {str(k):14s}", summ(g.alpha))

# 2) 같은 종목 안에서 짝 비교 — 두 상태를 다 겪은 종목만
print("\n[2] 같은 종목 안에서 (이월분 때 − 진입권 때)")
w = t.pivot(index="ticker", columns="carry", values="alpha").dropna()
d = w[True] - w[False]
print(f"   두 상태 다 겪은 종목 {len(d)}개, 차이", summ(d))
print("   이월분 쪽이 나은 종목", (d > 0).sum(), "/", len(d))
print(
    "   짝 t검정 p =",
    round(stats.ttest_1samp(d, 0).pvalue, 4),
    " 부호순위 p =",
    round(stats.wilcoxon(d).pvalue, 4),
)

# 3) 같은 날 안에서 짝 비교 — 그날 시장 분위기 걷어 내기
print("\n[3] 같은 발행일 안에서 (이월분 평균 − 진입권 평균)")
dd = df.groupby(["gd", "carry"]).alpha.mean().unstack().dropna()
d3 = dd[True] - dd[False]
print(f"   두 상태가 같이 있던 날 {len(d3)}일, 차이", summ(d3))
print("   이월분 쪽이 나은 날", (d3 > 0).sum(), "/", len(d3))
print(
    "   짝 t검정 p =",
    round(stats.ttest_1samp(d3, 0).pvalue, 4),
    " 부호순위 p =",
    round(stats.wilcoxon(d3).pvalue, 4),
)

# 4) 종목 고정효과 + 날짜 고정효과를 같이 걷어 낸 회귀 (이중 중심화 근사)
print("\n[4] 종목·날짜 효과를 같이 걷어 낸 회귀, 종목 묶음 표준오차")
x = df[["ticker", "gd", "alpha", "carry", "conv"]].copy()
x["c"] = x.carry.astype(float)
for _ in range(50):  # 교대 중심화로 두 고정효과 제거
    for col in ("alpha", "c", "conv"):
        x[col] = x[col] - x.groupby("ticker")[col].transform("mean")
        x[col] = x[col] - x.groupby("gd")[col].transform("mean")
for reg in ("c", "conv"):
    X, y = x[reg].values, x["alpha"].values
    beta = (X @ y) / (X @ X)
    resid = y - beta * X
    # 종목 묶음 표준오차
    meat = sum((X[g.index] @ resid[g.index]) ** 2 for _, g in x.groupby("ticker"))
    se = np.sqrt(meat) / (X @ X)
    label = "이월분 여부(1=이월)" if reg == "c" else "conviction(점수/100)"
    print(f"   {label}: 계수 {beta:+.3f}  묶음 표준오차 {se:.3f}  t={beta / se:+.2f}")

# 5) 에피소드 — 같은 종목이 연속 발행된 묶음의 첫 시트만
print("\n[5] 에피소드 첫 시트만 (같은 종목 연속 발행은 하나로)")
df = df.sort_values(["ticker", "gd"])
days = sorted(df.gd.unique())
idx = {d: i for i, d in enumerate(days)}
df["di"] = df.gd.map(idx)
df["new_ep"] = df.groupby("ticker").di.diff().fillna(99) > 1
first = df[df.new_ep]
print(f"   에피소드 {len(first)}개")
for k, g in first.groupby("carry"):
    print("  ", "이월분" if k else "진입권", summ(g.alpha))
