"""선별 × 청산 조합 탐색. 앞 기간(학습)에서 고르고 뒤 기간(검증)에 그대로 적용한다.

모든 (날짜, 종목) 후보의 청산 결과를 청산 설정별로 한 번씩 계산해 두고, 선별 조합은
그 표에서 골라 평균만 낸다. 성과 = 날짜별 동일가중 평균 (손익 − KODEX200 − 비용).
"""

import itertools
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

D = Path(__file__).parent
sys.path.insert(0, str(D))
import fastsim as fs  # noqa: E402

COST = 0.41  # 왕복: 수수료·세금 0.21 + 슬리피지 0.2
SPLIT = "2026-08-15"  # 이전 = 학습, 이후 = 검증

sc = pd.read_csv(D / "gscores.csv", dtype={"stock_code": str})
sc = sc[sc.score_date.isin(fs.DIDX) & (sc.score_date >= "2026-06-30")].copy()
FACT = ["mom", "qual", "val", "tech", "news", "sd", "sec"]
for f in FACT:
    sc[f"r_{f}"] = sc.groupby("score_date")[f].rank(pct=True).fillna(0.5)
sc["r_total"] = sc.groupby("score_date")["total"].rank(pct=True)
sc = sc.reset_index(drop=True)

SM = [
    {"type": "profit_floor", "activate_pct": 0.15, "floor_pct": 0.10},
    {"type": "trailing_tp", "activate_pct": 0.06, "drop_pct": 0.03},
    {"type": "breakeven", "activate_pct": 0.03, "floor_pct": 0.003},
    {"type": "death_cross", "ma_short": 5, "ma_long": 20, "min_loss_pct": 0.01},
    {"type": "fixed_sl", "pct": 0.05},
    {"type": "time_stop", "mode": "hold_days", "value": 5},
]
ED = [
    {"type": "overextension_exit", "rsi_threshold": 80.0},
    {"type": "trailing_tp", "activate_pct": 0.07, "drop_pct": 0.04},
    {"type": "fixed_sl", "pct": 0.05},
    {"type": "time_stop", "mode": "hold_days", "value": 10},
]


def hold(n):
    return [{"type": "time_stop", "mode": "hold_days", "value": n}]


_cache_path = D / "outcomes.pkl"
_cache = pickle.loads(_cache_path.read_bytes()) if _cache_path.exists() else {}


def outcomes(name, rules):
    """sc 행 순서에 맞춘 순알파 배열 (창 미종료면 NaN)."""
    if name in _cache:
        return _cache[name]
    a = np.full(len(sc), np.nan)
    for i, (c, d) in enumerate(zip(sc.stock_code, sc.score_date, strict=True)):
        o = fs.simulate(c, d, rules)
        if o is None:
            continue
        a[i] = o[1] - fs.bench(d, o[0]) - COST
    _cache[name] = a
    _cache_path.write_bytes(pickle.dumps(_cache))
    return a


def evaluate(score, alpha, n, gate_only, mask_dates=None):
    """날짜별 상위 n 의 평균 순알파 → (학습 평균, 학습 t, 검증 평균, 검증 t, 검증 일수)."""
    df = pd.DataFrame({"d": sc.score_date, "s": score, "a": alpha, "g": sc.gate})
    df = df[~np.isnan(df.a)]
    if gate_only:
        df = df[df.g == "open"]
    df = df.sort_values(["d", "s"], ascending=[True, False])
    top = df.groupby("d").head(n)
    daily = top.groupby("d").a.mean()
    out = []
    for part in (daily[daily.index < SPLIT], daily[daily.index >= SPLIT]):
        m = part.mean()
        t = m / part.std() * len(part) ** 0.5 if len(part) > 1 else np.nan
        out += [m, t, len(part)]
    return out


def weight_grid():
    for w in itertools.product([0, 1], [0, 1], [0, 1], [0, 1], [0, 1], [-1, 0, 1], [-1, 0, 1]):
        if any(w):
            yield dict(zip(["qual", "val", "sd", "news", "sec", "tech", "mom"], w, strict=True))


def score_of(w):
    s = np.zeros(len(sc))
    for f, v in w.items():
        if v:
            s += v * sc[f"r_{f}"].to_numpy()
    return s


if __name__ == "__main__":
    print(f"후보 {len(sc)} 행, 날짜 {sc.score_date.nunique()}")
