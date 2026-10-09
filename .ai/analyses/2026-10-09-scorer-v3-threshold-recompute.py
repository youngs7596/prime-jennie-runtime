"""@2 회차 점수에 RSI 부품·52주 고점 부품을 다시 계산해 붙이고 @3 총점을 만든다.

인자: CSV 폴더. scores.csv = @2 scout_runs × daily_quant_scores (회차 시각·게이트·
size_multiplier·서브점수·선정 여부), prices.csv = daily_prices(고가·종가). 일봉은 매일
16:00 에 들어오므로 장중 회차는 점수일 전날 종가까지 본 것으로 계산한다.
"""

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/youngs75/projects/prime-jennie-runtime")
from prime_jennie_runtime.slow_loop.scout.quant import _compute_rsi, _linear_map

D = sys.argv[1]
sc = pd.read_csv(f"{D}/scores.csv", dtype={"stock_code": str}, parse_dates=["gen_kst"])
px = pd.read_csv(f"{D}/prices.csv", dtype={"stock_code": str}, parse_dates=["price_date"])
px = px.sort_values(["stock_code", "price_date"])
by = {c: g for c, g in px.groupby("stock_code")}


def parts(code, d, is_bull):
    g = by.get(code)
    if g is None:
        return (np.nan,) * 3
    w = g[g.price_date < d].tail(150)
    closes = w.close_price.tolist()
    if len(closes) < 20:
        return (np.nan,) * 3
    rsi = _compute_rsi(closes, 14)
    if rsi is None:
        r = 2.5
    elif 40 <= rsi <= 70:
        r = 5.0
    elif 70 < rsi <= 80:
        r = 5.0 if is_bull else 3.0
    elif 30 <= rsi < 40:
        r = 3.5
    elif rsi < 30:
        r = 4.0
    else:
        r = 1.0
    # 옛 모멘텀 (EPS 보너스 빼고) — 검증용
    m = r
    if len(closes) >= 120:
        m += _linear_map((closes[-1] / closes[-120] - 1) * 100, -20, 30, 0, 5)
    elif len(closes) >= 60:
        m += _linear_map((closes[-1] / closes[-60] - 1) * 100, -15, 20, 0, 5)
    m += _linear_map((closes[-1] / closes[-20] - 1) * 100, -10, 15, 0, 5)
    if len(closes) >= 120:
        m6 = (closes[-1] / closes[-120] - 1) * 100
        m1 = (closes[-1] / closes[-20] - 1) * 100
        if m6 > 5 and m1 < -3:
            m += 5
        elif m6 > 0 and m1 < 0:
            m += 2.5
        elif m6 > 10 and m1 > 3:
            m += 3.5
    hi = w.high_price.max()
    dd = (closes[-1] / hi - 1) * 100
    p52 = 1.5 if dd < -30 else 3.5 if dd < -15 else 4.0 if dd < -5 else 5.0
    return r, min(20.0, m), p52


keys = sc[["score_date", "stock_code", "smul"]].copy()
keys["bull"] = keys.smul.fillna(0) >= 1.0
keys = keys.drop_duplicates(["score_date", "stock_code", "bull"])
res = [
    parts(c, pd.Timestamp(d), b)
    for d, c, b in zip(keys.score_date, keys.stock_code, keys.bull, strict=True)
]
keys[["rsi_part", "old_mom_noeps", "w52"]] = res
sc["bull"] = sc.smul.fillna(0) >= 1.0
sc = sc.merge(keys.drop(columns="smul"), on=["score_date", "stock_code", "bull"], how="left")

diff = (sc.mom - sc.old_mom_noeps).round(1)
print("옛 모멘텀 재현: 결측", sc.old_mom_noeps.isna().sum(), "/", len(sc))
print("  저장−재현 분포(상위):")
print(diff.value_counts().head(8))
print("가치−52주부품 음수:", ((sc.val - sc.w52) < -0.05).sum())

# 데이터 부족(중립) 종목: 옛 중립 momentum 10, value 10 → 새 2.5, 7.5
neutral = sc.rsi_part.isna()
sc["mom3"] = np.where(neutral, 2.5, sc.rsi_part)
sc["val3"] = np.where(neutral, sc.val - 2.5, sc.val - sc.w52)
raw = sc.mom3 + sc.qual + sc.val3 + sc.news + sc.sd + sc.sec
sc["total3"] = (raw * 100 / 70).clip(0, 100).round(1)
sc.to_csv(f"{D}/scores3.csv", index=False)
print(sc[["total", "total3"]].describe().round(2))
