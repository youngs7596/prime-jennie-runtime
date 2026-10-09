"""합의안 선별(학습 정보만) × 청산 규칙 조합. 학습에서 청산을 고르고 검증."""

import itertools
import sys

sys.path.insert(0, sys.argv[1])
import fastsim as fs
import grid as G
import numpy as np
import pandas as pd

pd.set_option("display.width", 220)
sc = G.sc
d = sc.score_date.to_numpy()
R = {f: sc[f"r_{f}"].to_numpy() for f in G.FACT}
W = {"qual": 1, "sd": 1, "val": 1, "sec": 1, "news": -1, "tech": -1, "mom": -1}
score = sum(v * R[f] for f, v in W.items())
base = G.outcomes("10일보유", G.hold(10))
common = ~np.isnan(base)


def run_exit(rules, n=5):
    """상위 n 만 시뮬 (빠름). 일별 평균 순알파·손익."""
    df = pd.DataFrame({"d": d, "s": score, "c": sc.stock_code})[common]
    top = df.sort_values(["d", "s"], ascending=[True, False]).groupby("d").head(n)
    rows = []
    for c, dd in zip(top.c, top.d):
        o = fs.simulate(c, dd, rules)
        if o is None:
            continue
        rows.append(
            {
                "d": dd,
                "pnl": o[1] - G.COST,
                "alpha": o[1] - fs.bench(dd, o[0]) - G.COST,
                "r": o[2],
                "hd": fs.DIDX[o[0]] - fs.DIDX[dd],
            }
        )
    t = pd.DataFrame(rows)
    day = t.groupby("d")[["pnl", "alpha"]].mean()
    tr, te = day[day.index < G.SPLIT], day[day.index >= G.SPLIT]
    return {
        "tr_a": tr.alpha.mean(),
        "te_a": te.alpha.mean(),
        "te_t": te.alpha.mean() / te.alpha.std() * len(te) ** 0.5,
        "tr_pnl": tr.pnl.mean(),
        "te_pnl": te.pnl.mean(),
        "보유일": t.hd.mean(),
        "손절비율": (t.r == "fixed_sl").mean(),
    }


def mk(sl, hold, trail, be, over):
    r = []
    if over:
        r.append({"type": "overextension_exit", "rsi_threshold": float(over)})
    if trail:
        r.append({"type": "trailing_tp", "activate_pct": trail[0], "drop_pct": trail[1]})
    if be:
        r.append({"type": "breakeven", "activate_pct": be, "floor_pct": 0.003})
    if sl:
        r.append({"type": "fixed_sl", "pct": sl})
    r.append({"type": "time_stop", "mode": "hold_days", "value": hold})
    return r


rows = []
for name, rules in [("SM(현행)", G.SM), ("ED(현행)", G.ED)]:
    rows.append({"설정": name, **run_exit(rules)})
for sl, hold, trail, be, over in itertools.product(
    [None, 0.03, 0.05, 0.07, 0.10],
    [1, 3, 5, 10],
    [None, (0.06, 0.03), (0.10, 0.05)],
    [None, 0.03],
    [None, 80, 85],
):
    rows.append(
        {
            "설정": f"손절{sl} 보유{hold} 추적{trail} 본전{be} 과열{over}",
            **run_exit(mk(sl, hold, trail, be, over)),
        }
    )
g = pd.DataFrame(rows)
g.to_csv(f"{sys.argv[1]}/stageD.csv", index=False)
print("현행 청산:")
print(g.head(2).round(2).to_string(index=False))
gg = g.iloc[2:].sort_values("tr_a", ascending=False)
print(
    f"\n청산 조합 {len(gg)}  학습 1위·상위10 의 검증 알파: {gg.te_a.iloc[0]:.2f} / {gg.head(10).te_a.mean():.2f}  전체 검증 평균 {gg.te_a.mean():.2f}  순위상관 {gg[['tr_a', 'te_a']].corr(method='spearman').iloc[0, 1]:.2f}"
)
print(gg.head(12).round(2).to_string(index=False))
print("\n요소별 평균 (학습/검증 알파):")
for key, pat in [
    ("손절", r"손절(\S+)"),
    ("보유", r"보유(\d+)"),
    ("과열", r"과열(\S+)"),
    ("본전", r"본전(\S+)"),
]:
    gg[key] = gg.설정.str.extract(pat)
    print(gg.groupby(key)[["tr_a", "te_a"]].mean().round(2).T.to_string())
