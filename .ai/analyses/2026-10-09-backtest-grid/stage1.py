"""@2 재현 선정 vs @3 가상 선정 → 시트 → paper 측정 시뮬레이터(일봉만) 로 성과 비교.

인자: 폴더 (scores3.csv, ohlcv.csv, sheets.csv, templates.jsonl).
"""

import json
import sys
from datetime import date
from importlib import import_module
from pathlib import Path

import pandas as pd

ROOT = "/home/youngs75/projects/prime-jennie-runtime"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/.ai/analyses")
from prime_jennie_runtime.jobs import paper_outcomes as po  # noqa: E402
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet  # noqa: E402

_rp = import_module("2026-10-09-scorer-v3-threshold-replay")

D = Path(sys.argv[1])
TODAY = date(2026, 10, 9)

px = pd.read_csv(D / "ohlcv.csv", dtype={"stock_code": str}, parse_dates=["price_date"])
px["price_date"] = px.price_date.dt.date
BY = {c: g.sort_values("price_date").to_dict("records") for c, g in px.groupby("stock_code")}
DATES = sorted(px.price_date.unique())


class Conn:
    async def fetch(self, sql, *a):
        if "minute_prices" in sql:
            return []
        t, s, e = a
        return [r for r in BY.get(t, []) if s <= r["price_date"] <= e]

    async def fetchval(self, sql, t, d):
        for r in BY.get(t, []):
            if r["price_date"] == d:
                return r["close_price"]
        return None

    async def fetchrow(self, sql, base, off):
        if "price_date > $1" in sql:
            xs = [x for x in DATES if x > base]
            return {"price_date": xs[off]} if off < len(xs) else None
        xs = [x for x in DATES if x < base][::-1]
        return {"price_date": xs[off]} if off < len(xs) else None


class Pool:
    def acquire(self):
        return self

    async def __aenter__(self):
        return Conn()

    async def __aexit__(self, *a):
        return False


TPL = {}
for line in (D / "templates.jsonl").read_text().splitlines():
    j = json.loads(line)
    TPL[j["strategy_tag"]] = j


def make_sheet(ticker, gen, tag, tpl=None):
    j = json.loads(json.dumps(tpl or TPL[tag]))
    j["ticker"] = ticker
    j["sheet_id"] = f"ps_{gen:%Y%m%d}_{ticker}_{gen:%H%M}"
    j["generated_at"] = gen.isoformat()
    j["valid_until"] = gen.replace(hour=15, minute=30, second=0, microsecond=0).isoformat()
    j["entry"]["valid_until"] = gen.isoformat()
    return PositionSheet.model_validate(j)


async def measure(sheets):
    out = []
    for s in sheets:
        o = await po._simulate_sheet(Pool(), s, today=TODAY)
        if o is None:
            continue
        out.append(o)
    return out


def bench(entry_d, exit_d):
    g = {r["price_date"]: r["close_price"] for r in BY["069500"]}
    if entry_d not in g or exit_d not in g:
        return None
    return (g[exit_d] / g[entry_d] - 1) * 100


def to_df(outs, extra):
    rows = []
    for o in outs:
        if o["exit_reason"] == "data_missing":
            continue
        b = bench(o["entry_date"], o["exit_date"])
        rows.append(
            {
                "sheet_id": o["sheet_id"],
                "entry_date": o["entry_date"],
                "pnl": float(o["pnl_pct"]),
                "bench": b,
                "reason": o["exit_reason"],
                **extra.get(o["sheet_id"], {}),
            }
        )
    df = pd.DataFrame(rows)
    df["alpha"] = df.pnl - df.bench
    return df


# ---- 선정 재현 → 시트 후보 ----
sc, runs = _rp.load(D)
sc["eps"] = (sc["mom"] - sc["old_mom_noeps"]).round(1) >= 0.5  # 옛 EPS 보너스 ≥1 ⇔ 상향 ≥5%


def sheets_from(sel):
    """열린 회차 순서대로, 그날 처음 선정된 종목에 시트 1 장."""
    tag = {(r, c): e for r, c, e in zip(sc.run_id, sc.stock_code, sc.eps, strict=True)}
    seen, res = set(), []
    for r, row in runs.iterrows():
        if row.gate != "open":
            continue
        gen = row.gen.to_pydatetime().replace(tzinfo=KST)
        for c in sorted(sel[r]):
            if (c, row.d) in seen:
                continue
            seen.add((c, row.d))
            res.append((c, gen, "EARNINGS_DRIFT" if tag.get((r, c)) else "SECTOR_MOMENTUM"))
    return res


sel2 = _rp.replay(sc, runs, "total", lambda d: (66.0, 59.0) if d < "2026-08-28" else (66.0, 62.0))
sel3 = _rp.replay(sc, runs, "total3", lambda d: (72.5, 68.5))
c2, c3 = sheets_from(sel2), sheets_from(sel3)

# ---- 검증: 실제 시트 vs 재현 @2 ----
act = pd.read_csv(D / "sheets.csv", dtype={"ticker": str}, parse_dates=["gen_kst"])
act["d"] = act.gen_kst.dt.date.astype(str)
a_keys = set(zip(act.ticker, act.d))
r_keys = {(c, g.date().isoformat()) for c, g, _ in c2}
print(f"실제 시트 종목-일 {len(a_keys)}, 재현 @2 {len(r_keys)}, 겹침 {len(a_keys & r_keys)}")
a_tag = {(t, d): s for t, d, s in zip(act.ticker, act.d, act.strategy_tag, strict=True)}
same = sum(
    a_tag[(c, g.date().isoformat())] == t for c, g, t in c2 if (c, g.date().isoformat()) in a_tag
)
print(f"  겹친 것 중 전략 태그 일치 {same}/{len(a_keys & r_keys)}")

# 실제 시트를 일봉만으로 재측정 → paper_outcomes(분봉 혼합) 와 대조
act_sheets = []
for t, g, tag, ej in zip(act.ticker, act.gen_kst, act.strategy_tag, act.exit_json, strict=True):
    tpl = json.loads(json.dumps(TPL[tag]))
    tpl["exit"] = json.loads(ej)
    s = make_sheet(t, g.to_pydatetime().replace(tzinfo=KST), tag, tpl)
    act_sheets.append(s)


import json as _j

with open(D / "to_sim.jsonl", "w") as f:
    for grp, items in [("act", None), ("v2", c2), ("v3", c3)]:
        if grp == "act":
            for s, tag in zip(act_sheets, act.strategy_tag, strict=True):
                f.write(
                    _j.dumps({"grp": grp, "tag": tag, "sheet": _j.loads(s.model_dump_json())})
                    + "\n"
                )
            continue
        for c, g, t in items:
            s = make_sheet(c, g, t)
            f.write(_j.dumps({"grp": grp, "tag": t, "sheet": _j.loads(s.model_dump_json())}) + "\n")
print("written")
