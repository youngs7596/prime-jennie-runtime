"""보유일 만료 매도 시각: 15:20(현행) vs 09:00(장 시작). 유니버스 전체와 합의안 상위5, 현행 청산 규칙(SM/ED) 그대로."""

import sys

sys.path.insert(0, sys.argv[1])
import fastsim as fs
import grid as G
import pandas as pd

sc = G.sc
R = {f: sc[f"r_{f}"].to_numpy() for f in G.FACT}
cons = sum(
    v * R[f]
    for f, v in {"qual": 1, "sd": 1, "val": 1, "sec": 1, "news": -1, "tech": -1, "mom": -1}.items()
)
rows = []
for name, rules in [("SM", G.SM), ("ED", G.ED), ("5일보유", G.hold(5)), ("1일보유", G.hold(1))]:
    for hm in (1520, 900):
        recs = []
        for c, d, s in zip(sc.stock_code, sc.score_date, cons):
            o = fs.simulate(c, d, rules, ts_hm=hm)
            if o is None:
                continue
            recs.append((d, s, o[1] - G.COST, o[1] - fs.bench(d, o[0]) - G.COST, o[2]))
        t = pd.DataFrame(recs, columns=["d", "s", "pnl", "a", "r"])
        top = t.sort_values(["d", "s"], ascending=[True, False]).groupby("d").head(5)
        for lab, x in [("유니버스", t), ("합의안5", top)]:
            day = x.groupby("d")[["pnl", "a"]].mean()
            tr, te = day[day.index < G.SPLIT], day[day.index >= G.SPLIT]
            rows.append(
                {
                    "청산": name,
                    "만료매도": hm,
                    "대상": lab,
                    "학습 손익": tr.pnl.mean(),
                    "검증 손익": te.pnl.mean(),
                    "학습 알파": tr.a.mean(),
                    "검증 알파": te.a.mean(),
                    "만료비율": (x.r == "time_stop").mean(),
                }
            )
print(pd.DataFrame(rows).round(2).to_string(index=False))
