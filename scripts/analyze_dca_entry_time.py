"""DCA 진입 시각 백테스트 — 005930·000660 의 장중 시간대별 평균 진입비용.

질문: 매 영업일 슬라이스를 "언제" 시장가로 사야 사는 사람한테 유리한가? 종목·금액이
고정이라 자유변수는 시각 하나뿐 → 과거 분봉으로 답이 나오는 측정 문제(예측 아님).

데이터: 시스템이 쓰는 Yahoo 차트 API 의 5분봉 60일치(us_market.py 와 같은 출처/헤더).
지표: 후보 시각의 가격을 그날 VWAP·종가·저가와 대비. 음수(VWAP 대비)면 평균보다 싸게
산 것. 14:30 대비 며칠이나 더 쌌는지(일관성)도 함께 본다.

실행: uv run python -m scripts.analyze_dca_entry_time
읽기 전용. 실주문·DB 쓰기 없음.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TICKERS = {"005930": "005930.KS", "000660": "000660.KS"}
_NAMES = {"005930": "삼성전자", "000660": "SK하이닉스"}

# 후보 집행 시각 (5분봉 시작시각). Yahoo KRX 장중 피드는 마감 전 ~30분(14:45 이후)을
# 안 줘서 신뢰 구간은 09:00~14:45 뿐 — 15:00~15:30 은 이 출처로 못 본다(KIS 분봉 필요).
_CANDIDATES = [
    "09:05",
    "09:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "12:00",
    "12:30",
    "13:00",
    "13:30",
    "14:00",
    "14:30",
    "14:45",
]
_BASELINE = "14:30"


async def fetch_5m(ticker: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        resp = await client.get(
            _URL.format(ticker=ticker), params={"range": "60d", "interval": "5m"}
        )
        resp.raise_for_status()
        return resp.json()


def parse_days(payload: dict) -> dict[str, dict[str, dict]]:
    """{date: {"HH:MM": {o,h,l,c,v}}} — 정규장(09:00~15:30) 5분봉만."""
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    off = int(meta.get("gmtoffset") or 32400)
    tz = timezone(timedelta(seconds=off))
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    days: dict[str, dict[str, dict]] = defaultdict(dict)
    for i, epoch in enumerate(ts):
        o, hi, lo, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
        if None in (o, hi, lo, c) or v is None:
            continue
        dt = datetime.fromtimestamp(epoch, tz)
        if not (
            datetime(dt.year, dt.month, dt.day, 9, 0, tzinfo=tz)
            <= dt
            <= datetime(dt.year, dt.month, dt.day, 15, 30, tzinfo=tz)
        ):
            continue
        days[dt.strftime("%Y-%m-%d")][dt.strftime("%H:%M")] = {
            "o": o,
            "h": hi,
            "l": lo,
            "c": c,
            "v": v,
        }
    return days


def day_stats(bars: dict[str, dict]) -> tuple[float, float]:
    """(VWAP, 저가) — 09:00~14:45 부분일 기준(Yahoo 가 마감 전 ~30분을 안 줌)."""
    num = den = 0.0
    low = float("inf")
    for hhmm in sorted(bars):
        if hhmm > "14:45":
            continue
        b = bars[hhmm]
        typical = (b["h"] + b["l"] + b["c"]) / 3
        num += typical * b["v"]
        den += b["v"]
        low = min(low, b["l"])
    vwap = num / den if den else 0.0
    return vwap, low


def _t_stat(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    se = (var / len(xs)) ** 0.5
    return mean / se if se else 0.0


def analyze(days: dict[str, dict[str, dict]]) -> list[tuple]:
    # 각 후보 시각의 일별 (가격/VWAP-1), (가격/저가-1), (가격/14:30-1) 누적.
    vs_vwap: dict[str, list[float]] = defaultdict(list)
    vs_low: dict[str, list[float]] = defaultdict(list)
    diff_baseline: dict[str, list[float]] = defaultdict(list)  # (P_t - P_1430)/P_1430

    n_days = 0
    for date in sorted(days):
        bars = days[date]
        if _BASELINE not in bars:
            continue
        vwap, low = day_stats(bars)
        if vwap <= 0 or low <= 0:
            continue
        base_px = bars[_BASELINE]["o"]
        n_days += 1
        for t in _CANDIDATES:
            if t not in bars:
                continue
            px = bars[t]["o"]
            vs_vwap[t].append(px / vwap - 1)
            vs_low[t].append(px / low - 1)
            diff_baseline[t].append(px / base_px - 1)

    def avg(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else float("nan")

    print(f"  분석 일수: {n_days} 거래일 (09:00~14:45, Yahoo 5분봉; 15:00 이후 데이터 없음)")
    print(
        f"  {'시각':>6} {'vsVWAP%':>9} {'vs저가%':>9} {'14:30比%':>9} "
        f"{'<14:30승률':>10} {'t(14:30比)':>10} {'n':>4}"
    )
    rows = []
    for t in _CANDIDATES:
        if not vs_vwap[t]:
            continue
        a_vwap = avg(vs_vwap[t]) * 100
        a_low = avg(vs_low[t]) * 100
        a_diff = avg(diff_baseline[t]) * 100
        winrate = (sum(1 for x in diff_baseline[t] if x < 0) / len(diff_baseline[t])) * 100
        tval = _t_stat(diff_baseline[t])
        rows.append((t, a_vwap, a_low, a_diff, winrate, tval, len(vs_vwap[t])))
        mark = "  ← 14:30" if t == _BASELINE else ""
        print(
            f"  {t:>6} {a_vwap:>+9.3f} {a_low:>+9.3f} {a_diff:>+9.3f} "
            f"{winrate:>9.1f}% {tval:>+10.2f} {len(vs_vwap[t]):>4}{mark}"
        )

    best = min(rows, key=lambda r: r[1])
    base = next(r for r in rows if r[0] == _BASELINE)
    print(
        f"\n  VWAP 대비 가장 싼 시각: {best[0]} ({best[1]:+.3f}%) vs 14:30 ({base[1]:+.3f}%). "
        f"{best[0]}−14:30 평균 {best[3]:+.3f}%, t≈{best[5]:+.2f} (|t|>2 면 노이즈 아닐 가능성)"
    )
    return rows


async def main() -> None:
    for code, yticker in _TICKERS.items():
        print(f"\n=== {_NAMES[code]} ({code}) ===")
        try:
            payload = await fetch_5m(yticker)
            days = parse_days(payload)
            if not days:
                print("  데이터 없음")
                continue
            analyze(days)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"  실패: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
