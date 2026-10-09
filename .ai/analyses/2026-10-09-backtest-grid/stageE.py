import sys

sys.path.insert(0, sys.argv[1])
import grid as G
import numpy as np
import pandas as pd

sc = G.sc
d = sc.score_date.to_numpy()
R = {f: sc[f"r_{f}"].to_numpy() for f in G.FACT}
cons = sum(
    v * R[f]
    for f, v in {"qual": 1, "sd": 1, "val": 1, "sec": 1, "news": -1, "tech": -1, "mom": -1}.items()
)
EX = {"SM(현행)": G.SM, "ED(현행)": G.ED, "5일보유": G.hold(5)}
A = {k: G.outcomes(k, v) for k, v in EX.items()}
common = np.logical_and.reduce(
    [~np.isnan(a) for a in A.values()] + [~np.isnan(G.outcomes("10일보유", G.hold(10)))]
)
for k, a in A.items():
    df = pd.DataFrame({"d": pd.to_datetime(d), "a": a, "cons": cons, "tot": sc.r_total.to_numpy()})[
        common
    ]
    uni = df.groupby("d").a.mean()
    out = {"유니버스(KODEX대비)": uni}
    for nm, col in [("합의안", "cons"), ("현행총점", "tot")]:
        out[nm + "−유니버스"] = (
            df.sort_values(["d", col], ascending=[True, False])
            .groupby("d")
            .head(5)
            .groupby("d")
            .a.mean()
            - uni
        )
    m = pd.DataFrame(out)
    print(f"\n[{k}] 상위5, 일별 평균을 반월 단위로 (단위 %p/건)")
    print(m.resample("SMS").mean().round(2).to_string())
    x = m["합의안−유니버스"]
    print(
        f"  합의안−유니버스 전체 {x.mean():.2f} (t={x.mean() / x.std() * len(x) ** 0.5:.1f}, 이긴 날 {(x > 0).mean() * 100:.0f}%)  현행총점−유니버스 {m['현행총점−유니버스'].mean():.2f}"
    )
