"""뉴스 부호까지 넣은 조합. 학습에서 고른 것이 검증에서 버티는지: 학습 1위·상위10·상위50 의 검증 평균 vs 전체 평균."""

import itertools
import sys

sys.path.insert(0, sys.argv[1])
import grid as G
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

pd.set_option("display.width", 220)
EXITS = {
    "SM(현행)": G.SM,
    "ED(현행)": G.ED,
    "1일보유": G.hold(1),
    "5일보유": G.hold(5),
    "10일보유": G.hold(10),
}
A = {k: G.outcomes(k, v) for k, v in EXITS.items()}
common = np.logical_and.reduce([~np.isnan(a) for a in A.values()])
sc = G.sc
d = sc.score_date.to_numpy()
R = {f: sc[f"r_{f}"].to_numpy() for f in G.FACT}


def excess_series(score, a, n):
    df = pd.DataFrame({"d": d, "s": score, "a": a})[common]
    uni = df.groupby("d").a.mean()
    top = (
        df.sort_values(["d", "s"], ascending=[True, False])
        .groupby("d")
        .head(n)
        .groupby("d")
        .a.mean()
    )
    return top - uni, top


def combos():
    names = ["qual", "val", "sd", "sec", "news", "tech", "mom"]
    for w in itertools.product([0, 1], [0, 1], [0, 1], [0, 1], [-1, 0, 1], [-1, 0, 1], [-1, 0, 1]):
        if any(w):
            yield dict(zip(names, w))


def lab(w):
    return " ".join(f"{f}{'+' if v > 0 else '-'}" for f, v in w.items() if v)


allw = list(combos())
summary = []
for k, a in A.items():
    rows = []
    for n in (5, 10):
        for w in allw:
            s = sum(v * R[f] for f, v in w.items() if v)
            ex, _ = excess_series(s, a, n)
            tr, te = ex[ex.index < G.SPLIT], ex[ex.index >= G.SPLIT]
            rows.append(
                {
                    "w": lab(w),
                    "n": n,
                    "tr": tr.mean(),
                    "te": te.mean(),
                    "te_t": te.mean() / te.std() * len(te) ** 0.5,
                }
            )
    g = pd.DataFrame(rows).sort_values("tr", ascending=False)
    g.to_csv(f"{sys.argv[1]}/stageC_{k}.csv", index=False)
    summary.append(
        {
            "청산": k,
            "조합수": len(g),
            "전체 검증평균": g.te.mean(),
            "학습1위 검증": g.te.iloc[0],
            "학습상위10 검증": g.te.head(10).mean(),
            "학습상위50 검증": g.te.head(50).mean(),
            "학습하위50 검증": g.te.tail(50).mean(),
            "순위상관": spearmanr(g.tr, g.te).correlation,
            "학습1위": f"{g.w.iloc[0]} n{g.n.iloc[0]}",
        }
    )
print(pd.DataFrame(summary).round(2).to_string(index=False))

# 학습 상위 50 안에 자주 나온 요인 부호 (청산 다섯 개 합산)
cnt = {}
for k in A:
    g = pd.read_csv(f"{sys.argv[1]}/stageC_{k}.csv").head(50)
    for w in g.w:
        for t in str(w).split():
            cnt[t] = cnt.get(t, 0) + 1
print(
    "\n학습 상위 50 에 나온 요인 부호 빈도(청산 5개 합, 최대 250):",
    dict(sorted(cnt.items(), key=lambda x: -x[1])),
)
