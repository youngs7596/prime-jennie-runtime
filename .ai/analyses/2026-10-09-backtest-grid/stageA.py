import sys
import time

sys.path.insert(0, sys.argv[1])
import grid as G
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

pd.set_option("display.width", 220)
EXITS = {
    "SM(현행)": G.SM,
    "ED(현행)": G.ED,
    "5일보유": G.hold(5),
    "10일보유": G.hold(10),
    "1일보유": G.hold(1),
}
for name, rules in EXITS.items():
    t = time.time()
    a = G.outcomes(name, rules)
    allu = G.evaluate(np.zeros(len(a)), a, 10**6, False)
    print(
        f"\n=== 청산 {name}  ({time.time() - t:.0f}s)  유니버스 전체: 학습 {allu[0]:.2f} / 검증 {allu[3]:.2f} (일수 {allu[2]}/{allu[5]})"
    )
    rows = []
    for n in (5, 10, 20):
        for gate in (False, True):
            b = G.evaluate(G.sc.r_total.to_numpy(), a, n, gate)
            rows.append(
                {
                    "w": "현행총점",
                    "n": n,
                    "gate": gate,
                    "tr": b[0],
                    "tr_t": b[1],
                    "te": b[3],
                    "te_t": b[4],
                }
            )
            for w in G.weight_grid():
                r = G.evaluate(G.score_of(w), a, n, gate)
                rows.append(
                    {
                        "w": " ".join(f"{k}{'+' if v > 0 else '-'}" for k, v in w.items() if v),
                        "n": n,
                        "gate": gate,
                        "tr": r[0],
                        "tr_t": r[1],
                        "te": r[3],
                        "te_t": r[4],
                    }
                )
    df = pd.DataFrame(rows)
    base = df[df.w == "현행총점"]
    print(base.round(2).to_string(index=False))
    g = df[df.w != "현행총점"]
    rho = spearmanr(g.tr, g.te).correlation
    print(
        f"조합 {len(g)}: 학습 양수 {(g.tr > 0).mean() * 100:.0f}%  검증 양수 {(g.te > 0).mean() * 100:.0f}%  학습·검증 순위상관 {rho:.2f}"
    )
    top = g.sort_values("tr", ascending=False).head(10)
    print("학습 상위 10 → 검증:")
    print(top.round(2).to_string(index=False))
    df.to_csv(f"{sys.argv[1]}/stageA_{name}.csv", index=False)
