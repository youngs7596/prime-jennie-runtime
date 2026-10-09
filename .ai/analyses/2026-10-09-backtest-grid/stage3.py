import json
import sys

import pandas as pd

D = sys.argv[1]
pd.set_option("display.width", 200)
px = pd.read_csv(f"{D}/ohlcv.csv", dtype={"stock_code": str})
k = px[px.stock_code == "069500"].set_index("price_date").close_price
rows = [json.loads(l) for l in open(f"{D}/sim_out.jsonl")]
df = pd.DataFrame(rows)
df = df[df.exit_reason != "data_missing"].copy()
df["pnl"] = df.pnl_pct.astype(float)
df["bench"] = [(k[b] / k[a] - 1) * 100 for a, b in zip(df.entry_date, df.exit_date)]
df["alpha"] = df.pnl - df.bench
df["tk"] = df.sheet_id.str.split("_").str[2]
df["key"] = df.tk + "_" + df.entry_date
df.to_csv(f"{D}/sim_out.csv", index=False)

act = pd.read_csv(f"{D}/sheets.csv", dtype={"ticker": str}).dropna(subset=["pnl_pct"])
print(f"운영 paper_outcomes: n={len(act)} pnl {act.pnl_pct.mean():.2f}")
a = df[df.grp == "act"]
print(f"같은 실제 시트 재측정: n={len(a)} pnl {a.pnl.mean():.2f}")


def summ(g, name):
    n = len(g)
    t = g.alpha.mean() / g.alpha.std() * n**0.5
    print(
        f"{name:14s} n={n:4d} pnl {g.pnl.mean():6.2f} 승률 {(g.pnl > 0).mean() * 100:4.1f}% "
        f"bench {g.bench.mean():5.2f} alpha {g.alpha.mean():6.2f} (t={t:4.1f}) 벤치상회 {(g.alpha > 0).mean() * 100:4.1f}%"
    )


print()
for grp, name in [("act", "실제 시트"), ("v2", "@2 재현"), ("v3", "@3 가상")]:
    summ(df[df.grp == grp], name)

v2, v3 = df[df.grp == "v2"], df[df.grp == "v3"]
both = set(v2.key) & set(v3.key)
print(f"\n겹치는 종목-일 {len(both)} / @2 {len(v2)} / @3 {len(v3)}")
summ(v2[v2.key.isin(both)], "공통")
summ(v2[~v2.key.isin(both)], "@2 에만")
summ(v3[~v3.key.isin(both)], "@3 에만")

# 같은 날 묶어 일별 평균 차 (날짜 효과 제거)
d2 = v2.groupby("entry_date").alpha.mean()
d3 = v3.groupby("entry_date").alpha.mean()
dd = (d3 - d2).dropna()
print(
    f"\n일별 평균 alpha 차 (@3−@2): {dd.mean():.2f}%p, n일={len(dd)}, t={dd.mean() / dd.std() * len(dd) ** 0.5:.1f}, @3 우위 일수 {(dd > 0).sum()}"
)
# 주별
w = pd.DataFrame({"v2": d2, "v3": d3})
w.index = pd.to_datetime(w.index)
print(w.resample("W").mean().round(2).to_string())
for grp in ["v2", "v3"]:
    g = df[df.grp == grp]
    print(f"\n[{grp}] 전략·청산")
    print(
        g.groupby("tag")
        .agg(n=("pnl", "size"), pnl=("pnl", "mean"), alpha=("alpha", "mean"))
        .round(2)
        .to_string()
    )
    print(g.groupby("exit_reason").agg(n=("pnl", "size"), pnl=("pnl", "mean")).round(2).to_string())
