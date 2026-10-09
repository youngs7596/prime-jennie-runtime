"""수시공시 사건 연구: 공시일 d. 반응일(d−1 종가→d 종가), 진입은 d+1 시가 → d+1 종가 / d+5 종가.
초과 = 종목 − 같은 날 코스피 보통주 동일가중. 비용 미차감(사건 크기부터 본다)."""

import re
import sys

import numpy as np
import pandas as pd

D = sys.argv[1]
pd.set_option("display.width", 220)
codes = set(open(f"{D}/kospi_codes.txt").read().split())
px = pd.read_csv(f"{D}/ohlcv_all.csv", dtype={"stock_code": str})
px = px[px.stock_code.isin(codes)]
C = (
    px.pivot(index="price_date", columns="stock_code", values="close_price")
    .sort_index()
    .astype(float)
)
O = (
    px.pivot(index="price_date", columns="stock_code", values="open_price")
    .sort_index()
    .astype(float)
)
C = C[C.index >= "2026-07-01"]
O = O.loc[C.index]
dates = list(C.index)
di = {d: i for i, d in enumerate(dates)}
mk_c = C.pct_change(fill_method=None).mean(axis=1)  # 동일가중 일수익
ds = pd.read_csv(f"{D}/disc.csv", dtype={"stock_code": str})
CATS = [
    ("공급계약", "공급계약"),
    ("잠정실적", r"잠정\)?실적|영업\(잠정\)"),
    ("유상증자", "유상증자결정"),
    ("무상증자", "무상증자결정"),
    ("자사주취득", r"자기주식\s*취득\s*결정|자기주식취득신탁"),
    ("자사주소각", "소각"),
    ("배당", "배당"),
    ("CB·BW", "전환사채|신주인수권부사채|교환사채"),
    ("최대주주변경", r"최대주주\s*변경"),
    ("타법인주식", r"타법인\s*주식"),
    ("밸류업", r"기업가치\s*제고"),
    ("소송", "소송"),
    ("조회공시", "조회공시"),
    ("임원·주요주주 지분", "임원ㆍ주요주주|임원·주요주주|주식등의대량보유"),
    ("정기보고서", r"사업보고서|반기보고서|분기보고서"),
    ("기타", "."),
]


def cat(t):
    t = re.sub(r"^\[[^\]]*\]", "", t)
    for n, p in CATS:
        if re.search(p, t):
            return n


ds["cat"] = ds.title.map(cat)
ds["is_fix"] = ds.title.str.contains(r"\[기재정정\]|\[첨부")
ds = ds[~ds.is_fix].drop_duplicates(["stock_code", "disclosure_date", "cat"])
rows = []
for d, c, k in zip(ds.disclosure_date, ds.stock_code, ds.cat):
    if d not in di or c not in C:
        continue
    i = di[d]
    if i + 5 >= len(dates) or i < 1:
        continue
    cp, c0 = C[c].iloc[i - 1], C[c].iloc[i]
    o1, c1, c5 = O[c].iloc[i + 1], C[c].iloc[i + 1], C[c].iloc[i + 5]
    if np.isnan([cp, c0, o1, c1, c5]).any():
        continue
    m0 = mk_c.iloc[i]
    m1 = mk_c.iloc[i + 1]
    m5 = (1 + mk_c.iloc[i + 1 : i + 6]).prod() - 1
    rows.append(
        {
            "cat": k,
            "d": d,
            "react": (c0 / cp - 1 - m0) * 100,
            "gap": (o1 / c0 - 1) * 100,
            "t1": (c1 / o1 - 1 - m1) * 100,
            "t5": (c5 / o1 - 1 - m5) * 100,
        }
    )
E = pd.DataFrame(rows)


def tstat(x):
    return x.mean() / x.std() * len(x) ** 0.5 if len(x) > 2 else np.nan


g = E.groupby("cat").agg(
    n=("t5", "size"),
    반응일=("react", "mean"),
    다음날갭=("gap", "mean"),
    다음날장중=("t1", "mean"),
    d1시가부터5일=("t5", "mean"),
    t5_t=("t5", tstat),
    승률=("t5", lambda x: (x > 0).mean()),
)
print(g.sort_values("n", ascending=False).round(2).to_string())
E.to_csv(f"{D}/disc_events.csv", index=False)
