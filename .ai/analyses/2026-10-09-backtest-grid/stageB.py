"""선별 실력 = 같은 날 유니버스 평균(같은 청산) 대비 초과분. 공통 날짜(10일 창이 닫힌 날)만."""

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
common = ~np.isnan(A["10일보유"])
for k in A:
    common &= ~np.isnan(A[k])
sc = G.sc
d = sc.score_date.to_numpy()


def excess(score, a, n, gate):
    df = pd.DataFrame({"d": d, "s": score, "a": a, "g": sc.gate})[common]
    if gate:
        df = df[df.g == "open"]
    uni = df.groupby("d").a.mean()
    top = (
        df.sort_values(["d", "s"], ascending=[True, False])
        .groupby("d")
        .head(n)
        .groupby("d")
        .a.mean()
    )
    ex = top - uni
    res = []
    for part in (ex[ex.index < G.SPLIT], ex[ex.index >= G.SPLIT]):
        res += [part.mean(), part.mean() / part.std() * len(part) ** 0.5, len(part)]
    return res


print("공통 날짜", len(set(d[common])), "학습", len({x for x in d[common] if x < G.SPLIT}))
print("\n청산 설정별 유니버스 전체 순알파(KODEX200 대비, 비용 차감) — 고르는 것과 무관한 청산 효과")
for k, a in A.items():
    df = pd.DataFrame({"d": d, "a": a})[common].groupby("d").a.mean()
    print(
        f"  {k:8s} 학습 {df[df.index < G.SPLIT].mean():6.2f}  검증 {df[df.index >= G.SPLIT].mean():6.2f}"
    )

print("\n단일 요인 상위 10 의 유니버스 대비 초과 (학습 / 검증), 게이트 무시")
rows = []
for f in G.FACT + ["total"]:
    for sign in (1, -1):
        r = {"요인": f"{f}{'+' if sign > 0 else '-'}"}
        for k, a in A.items():
            e = excess(sign * sc[f"r_{f}"].to_numpy(), a, 10, False)
            r[k] = f"{e[0]:5.2f}/{e[3]:5.2f}"
        rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

for k in ["SM(현행)", "ED(현행)", "5일보유"]:
    a = A[k]
    rows = []
    for n in (5, 10, 20):
        for w in G.weight_grid():
            e = excess(G.score_of(w), a, n, False)
            rows.append(
                {
                    "w": " ".join(f"{f}{'+' if v > 0 else '-'}" for f, v in w.items() if v),
                    "n": n,
                    "tr": e[0],
                    "tr_t": e[1],
                    "te": e[3],
                    "te_t": e[4],
                }
            )
    g = pd.DataFrame(rows)
    print(
        f"\n=== {k}: 조합 {len(g)}  학습 양수 {(g.tr > 0).mean() * 100:.0f}%  검증 양수 {(g.te > 0).mean() * 100:.0f}%  순위상관 {spearmanr(g.tr, g.te).correlation:.2f}"
    )
    b = [excess(sc.r_total.to_numpy(), a, n, False) for n in (5, 10, 20)]
    print("  현행총점 n5/10/20 학습·검증:", [(round(x[0], 2), round(x[3], 2)) for x in b])
    print(g.sort_values("tr", ascending=False).head(8).round(2).to_string(index=False))
    g.to_csv(f"{sys.argv[1]}/stageB_{k}.csv", index=False)
