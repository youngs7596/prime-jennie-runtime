"""저장 점수로 MA3 + 히스테리시스 + 상한 20 선정을 재현한다 (deterministic_scout 와 같은 규칙).

인자: CSV 폴더(scores3.csv). 단독 실행하면 현행 문턱으로 실제 선정과 대조한다.
"""

import sys

import pandas as pd


def load(d):
    sc = pd.read_csv(f"{d}/scores3.csv", dtype={"stock_code": str}, parse_dates=["gen_kst"])
    sc = sc[sc.is_active == "t"]
    runs = (
        sc.groupby("run_id")
        .agg(gen=("gen_kst", "first"), gate=("gate", "first"), d=("score_date", "first"))
        .sort_values("gen")
    )
    return sc, runs


def replay(sc, runs, col, th):
    """th(score_date) -> (entry, exit). 회차별 선정 집합을 돌려준다."""
    tot = {r: dict(zip(g.stock_code, g[col], strict=True)) for r, g in sc.groupby("run_id")}
    sel = {}
    open_hist = []  # 열린 회차 id (오래된→최신)
    for r, row in runs.iterrows():
        entry, exit_ = th(row.d)
        hist_runs = open_hist[-2:]
        prev = sel.get(hist_runs[-1]) if hist_runs else None
        prev = prev if prev else None
        ma = {}
        for c, v in tot[r].items():
            past = [tot[h][c] for h in hist_runs if c in tot[h]]
            xs = (past + [v])[-3:]
            ma[c] = round(sum(xs) / len(xs), 1)
        surv = []
        for c, m in ma.items():
            if prev is None:
                if m >= entry:
                    surv.append((c, m))
                continue
            if m >= entry or (m >= exit_ and c in prev):
                surv.append((c, m))
        surv.sort(key=lambda x: x[1], reverse=True)
        sel[r] = {c for c, _ in surv[:20]}
        sel[r + "#passed"] = len(surv)
        sel[r + "#carry"] = sum(1 for c, m in surv[:20] if m < entry)
        if row.gate == "open":
            open_hist.append(r)
    return sel


if __name__ == "__main__":
    D = sys.argv[1]
    sc, runs = load(D)

    def old(d):
        return (66.0, 59.0) if d < "2026-08-28" else (66.0, 62.0)

    sel = replay(sc, runs, "total", old)
    actual = {r: set(g.stock_code[g.sel == "t"]) for r, g in sc.groupby("run_id")}
    o = runs[runs.gate == "open"]
    match = sum(sel[r] == actual[r] for r in o.index)
    print(f"열린 회차 {len(o)} 중 실제 선정과 일치 {match}")
    bad = [r for r in o.index if sel[r] != actual[r]][:10]
    for r in bad:
        print(r, len(sel[r]), len(actual[r]), sorted(sel[r] ^ actual[r])[:6])
