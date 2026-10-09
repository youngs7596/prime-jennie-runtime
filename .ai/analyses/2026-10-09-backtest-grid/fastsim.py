"""운영 paper 측정(jobs/paper_outcomes._simulate_sheet + fast_loop.exit_evaluator)을 메모리에서
그대로 재현하는 빠른 시뮬레이터. 규칙 평가 순서·상태 변화·분봉 RSI(하루마다 새로 계산)·
일봉 4-tick 대체·창 종료 처리까지 운영 코드와 같다. 청산 규칙은 dict 목록으로 받는다.
"""

import gzip
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/youngs75/projects/prime-jennie-runtime")
from prime_jennie_runtime.fast_loop.indicators import check_death_cross  # noqa: E402

D = Path(__file__).parent
MIN_MINUTE_BARS = 300
DAILY_TICK_HM = (905, 1030, 1400, 1530)


def _load():
    cache = D / "cache.pkl"
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    px = pd.read_csv(D / "ohlcv.csv", dtype={"stock_code": str})
    dates = sorted(px.price_date.unique())
    daily = {}
    for c, g in px.groupby("stock_code"):
        g = g.sort_values("price_date")
        daily[c] = {
            d: (float(o), float(h), float(lo), float(cl))
            for d, o, h, lo, cl in zip(
                g.price_date, g.open_price, g.high_price, g.low_price, g.close_price, strict=True
            )
        }
    mn = pd.read_csv(gzip.open(D / "minute.csv.gz"), dtype={"stock_code": str})
    mn["d"] = mn.ts.str[:10]
    mn["hm"] = mn.ts.str[11:13].astype(int) * 100 + mn.ts.str[14:16].astype(int)
    minute = {}
    for (c, d), g in mn.groupby(["stock_code", "d"], sort=False):
        closes = g.close_price.astype(float).tolist()
        if len(closes) < MIN_MINUTE_BARS:
            continue
        rsi = _rsi(closes)
        minute[(c, d)] = (closes, g.hm.tolist(), rsi)
    data = {"dates": dates, "daily": daily, "minute": minute}
    cache.write_bytes(pickle.dumps(data))
    return data


def _rsi(closes, period=14):
    """compute_rsi(closes[:i+1]) 를 i 마다 부른 것과 같은 값 (Wilder, 누적)."""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        dlt = closes[i] - closes[i - 1]
        gains.append(max(dlt, 0.0))
        losses.append(abs(min(dlt, 0.0)))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    def val(ag, al):
        return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    out[period] = val(ag, al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = val(ag, al)
    return out


DATA = _load()
DATES = DATA["dates"]
DIDX = {d: i for i, d in enumerate(DATES)}
DAILY = DATA["daily"]
MINUTE = DATA["minute"]


def hold_days(rules):
    best = 0
    for r in rules:
        if r["type"] != "time_stop":
            continue
        if r.get("mode") == "eod":
            best = max(best, 1)
        elif r.get("value") is not None:
            best = max(best, int(r["value"]))
    return best if best > 0 else 20


def simulate(ticker, entry_date, rules, last_date="2026-10-08", ts_hm=1520):
    """반환: (exit_date, pnl_pct, reason) 또는 None(창 미종료/데이터 없음)."""
    dd = DAILY.get(ticker)
    if dd is None or entry_date not in dd:
        return None
    entry = dd[entry_date][3]
    i0 = DIDX[entry_date]
    n = hold_days(rules)
    if i0 + n >= len(DATES) or DATES[i0 + n] > last_date:
        return None
    dc = next((r for r in rules if r["type"] == "death_cross"), None)
    if dc:
        hs = max(0, i0 - (dc["ma_long"] + 5))
        hist = [dd[d][3] for d in DATES[hs : i0 + 1] if d in dd]
    else:
        hist = [entry]
    sim_days = [d for d in DATES[i0 + 1 : i0 + n + 1] if d in dd]
    if not sim_days:
        return None
    hw, peak = entry, 0.0
    trail_on = pf_on = be_on = over_done = False
    be_price = None
    dc_checked = None
    closes_so_far = list(hist)
    for k, day in enumerate(sim_days, start=1):
        closes_so_far.append(dd[day][3])
        m = MINUTE.get((ticker, day))
        if m is not None:
            prices, hms, rsis = m
        else:
            o, h, lo, c = dd[day]
            prices, hms, rsis = (o, h, lo, c), DAILY_TICK_HM, (None,) * 4
        for p, hm, rsi in zip(prices, hms, rsis, strict=True):
            if p > hw:
                hw = p
                peak = p / entry - 1.0
            cr = p / entry - 1.0
            for r in rules:
                t = r["type"]
                if t == "fixed_sl":
                    th = entry * (1.0 - r["pct"])
                    if be_price is not None and be_price > th:
                        th = be_price
                    if p <= th:
                        return day, (p - entry) / entry * 100, "fixed_sl"
                elif t == "trailing_tp":
                    if not trail_on:
                        if cr >= r["activate_pct"]:
                            trail_on = True
                        else:
                            continue
                    if p < hw * (1.0 - r["drop_pct"]):
                        return day, (p - entry) / entry * 100, "trailing_tp"
                elif t == "breakeven":
                    if not be_on:
                        if cr >= r["activate_pct"]:
                            be_on = True
                            be_price = entry * (1.0 + r["floor_pct"])
                        else:
                            continue
                    if p <= be_price:
                        return day, (p - entry) / entry * 100, "breakeven"
                    break  # 운영 코드: SL 상향 결정(should_close=False)을 돌려줘 뒤 규칙을 안 본다
                elif t == "time_stop":
                    if r.get("mode") == "eod":
                        if hm >= ts_hm:
                            return day, (p - entry) / entry * 100, "time_stop"
                    elif k >= r["value"] and hm >= ts_hm:
                        return day, (p - entry) / entry * 100, "time_stop"
                elif t == "overextension_exit":
                    if not over_done and rsi is not None and rsi >= r["rsi_threshold"]:
                        over_done = True
                        return day, (p - entry) / entry * 100, "overextension_exit"
                elif t == "profit_floor":
                    if not pf_on:
                        if peak >= r["activate_pct"]:
                            pf_on = True
                        else:
                            continue
                    if cr < r["floor_pct"]:
                        return day, (p - entry) / entry * 100, "profit_floor"
                elif t == "death_cross":
                    if dc_checked == day:
                        continue
                    if not cr <= -r["min_loss_pct"]:
                        continue
                    dc_checked = day
                    if check_death_cross(closes_so_far, short=r["ma_short"], long=r["ma_long"]):
                        return day, (p - entry) / entry * 100, "death_cross"
                elif t == "fixed_tp":
                    if cr >= r["pct"]:
                        return day, (p - entry) / entry * 100, "fixed_tp"
    last = sim_days[-1]
    return last, (dd[last][3] - entry) / entry * 100, "window_expired"


def bench(entry_date, exit_date):
    k = DAILY["069500"]
    return (k[exit_date][3] / k[entry_date][3] - 1) * 100


if __name__ == "__main__":
    print(len(DATES), len(DAILY), len(MINUTE))
    _ = np
