"""DCA 백테스트 — 1절 백필 + 2절 검증 게이트.

소스 결정 근거:
- 개별주 일봉은 시스템상 KIS 게이트웨이 경로(naver 크롤러는 지수 전용)라, 6년 백필엔
  Yahoo chart API 를 캐논으로 쓴다. Yahoo 는 수정종가(adjclose)를 네이티브로 주고
  2020-01 부터 전 구간이 있으며, 시스템 KOSPI(-9.99%)와 이미 교차검증됐다.
- 네이버 fchart(종목 일봉)를 독립 2차 소스로 같이 받아 폭락일 종가를 교차검증한다.
- 거래 체결가 = 원종가(close). 2020-2026 구간엔 삼성/하이닉스 액면분할이 없어
  원종가가 이미 연속이다(아래 검증이 이를 확인). adjclose 는 분할/배당 탐지용으로만 둔다.
"""
from __future__ import annotations
import urllib.request, urllib.parse, json, csv, datetime as dt, sys
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HDR = {"User-Agent": "Mozilla/5.0"}
HERE = Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)

INSTR = {"005930.KS": "samsung", "000660.KS": "hynix", "^KS11": "kospi"}
NAVER = {"005930": "samsung", "000660": "hynix"}
START = dt.datetime(2020, 1, 1, tzinfo=KST)
END = dt.datetime.now(KST) + dt.timedelta(days=1)


def _get(url: str, decode: str | None = None) -> str | bytes:
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=20).read()
    return raw.decode(decode, "replace") if decode else raw


def fetch_yahoo(sym: str) -> list[dict]:
    p1, p2 = int(START.timestamp()), int(END.timestamp())
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
         f"?period1={p1}&period2={p2}&interval=1d&includeAdjustedClose=true")
    d = json.loads(_get(u))["chart"]["result"][0]
    ts = d["timestamp"]
    q = d["indicators"]["quote"][0]
    adj = d["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    rows = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        date = dt.datetime.fromtimestamp(t, KST).date()
        rows.append({
            "date": date.isoformat(),
            "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
            "close": c, "adjclose": adj[i], "volume": q["volume"][i],
        })
    return rows


def fetch_naver_close(code: str) -> dict[str, float]:
    """네이버 fchart 종목 일봉 — date→close (교차검증용)."""
    u = (f"https://fchart.stock.naver.com/sise.nhn?symbol={code}"
         f"&timeframe=day&count=2200&requestType=0")
    raw = _get(u, "euc-kr")
    out = {}
    for line in raw.split("<item data=\"")[1:]:
        body = line.split("\"")[0]
        parts = body.split("|")
        if len(parts) >= 5:
            d = parts[0]
            out[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = float(parts[4])
    return out


def save_csv(name: str, rows: list[dict]) -> None:
    path = DATA / f"{name}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "adjclose", "volume"])
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    print("=== 1절 백필 (Yahoo) ===")
    data = {}
    for sym, name in INSTR.items():
        rows = fetch_yahoo(sym)
        save_csv(name, rows)
        data[name] = rows
        print(f"  {name:8s} ({sym:10s}) n={len(rows)}  {rows[0]['date']} ~ {rows[-1]['date']}")

    print("\n=== 2절 검증 게이트 ===")
    fail = []

    # --- 2.1 폭락일 실측 + 네이버 교차검증 ---
    print("\n[2.1] 알려진 폭락일 종가 (Yahoo close / adjclose, 네이버 close)")
    naver = {name: fetch_naver_close(code) for code, name in NAVER.items()}
    probe = ["2020-03-19", "2024-08-05", "2025-04-07", "2026-06-23"]
    idx = {name: {r["date"]: r for r in rows} for name, rows in data.items()}
    for d in probe:
        line = [f"  {d}:"]
        for name in ("samsung", "hynix", "kospi"):
            r = idx[name].get(d)
            if not r:
                line.append(f"{name}=N/A")
                continue
            s = f"{name}={r['close']:.0f}/{r['adjclose']:.0f}"
            if name in naver:
                nv = naver[name].get(d)
                if nv is not None:
                    diff = abs(nv - r["close"]) / r["close"] * 100
                    s += f"(naver {nv:.0f},Δ{diff:.2f}%)"
                    if diff > 1.0:
                        fail.append(f"naver-yahoo 불일치 {name} {d}: {diff:.2f}%")
            line.append(s)
        print("   " + "  ".join(line))

    # KOSPI 일간 등락 — -4% 룰이 잡는 날 미리보기
    print("\n[2.1b] KOSPI 일간 -4% 이하 날 (이벤트 후보)")
    krows = data["kospi"]
    for i in range(1, len(krows)):
        pc = krows[i - 1]["close"]
        chg = (krows[i]["close"] - pc) / pc * 100
        if chg <= -4.0:
            print(f"   {krows[i]['date']}  {chg:+.2f}%")

    # --- 2.2 이상치 스캔 ---
    print("\n[2.2] 이상치 스캔 (null/0/음수/전일대비 ±50% 초과)")
    for name, rows in data.items():
        nulls = sum(1 for r in rows if r["close"] is None)
        zeros = sum(1 for r in rows if r["close"] == 0)
        negs = sum(1 for r in rows if r["close"] < 0)
        jumps = []
        for i in range(1, len(rows)):
            pc = rows[i - 1]["close"]
            if pc and abs(rows[i]["close"] - pc) / pc > 0.50:
                jumps.append((rows[i]["date"], rows[i - 1]["close"], rows[i]["close"]))
        print(f"   {name:8s} null={nulls} zero={zeros} neg={negs} jump>50%={len(jumps)} {jumps if jumps else ''}")
        if nulls or zeros or negs:
            fail.append(f"{name} 이상치 null/zero/neg")
        # 분할 의심 점프는 게이트 실패로 — 수동 확인 필요
        if jumps:
            fail.append(f"{name} 50%+ 점프(분할 의심): {jumps}")

    # --- 2.3 수정주가 연속성 (adjclose/close 비율 점프) ---
    print("\n[2.3] 분할/증자 경계 점검 (adjclose/close 비율의 급변)")
    for name in ("samsung", "hynix"):
        rows = data[name]
        ratios = [(r["date"], r["adjclose"] / r["close"]) for r in rows if r["close"]]
        big = []
        for i in range(1, len(ratios)):
            if ratios[i - 1][1] and abs(ratios[i][1] - ratios[i - 1][1]) / ratios[i - 1][1] > 0.10:
                big.append((ratios[i][0], round(ratios[i - 1][1], 4), round(ratios[i][1], 4)))
        print(f"   {name:8s} 비율 {ratios[0][1]:.4f}→{ratios[-1][1]:.4f}, 10%+급변={len(big)} {big if big else ''}")
        # 배당 누적은 완만해 OK. 10%+ 급변은 분할/증자 신호 → 보고
        if big:
            fail.append(f"{name} adjclose 비율 급변(분할/증자 의심): {big}")

    print("\n=== 게이트 결과 ===")
    if fail:
        print("  ❌ FAIL:")
        for f in fail:
            print(f"    - {f}")
        sys.exit(1)
    print("  ✅ PASS — 백테스트 진행 가능")


if __name__ == "__main__":
    main()
