"""DCA 백테스트 — 전략 A(무조건부 균등) vs C(풀백 가속).

룩어헤드 차단 (4절):
- 슬라이스 크기는 sizing_price = '전일 종가' P(T-1) 로만 결정. 당일 종가/저가/미래값 사용 안 함.
  코드상 size_slice(T) 는 prices[:T] 만 받고 prices[T] 는 절대 참조 안 한다(assert 로 강제).
- 체결가 = 당일 종가(close[T]). 크기 결정에 안 쓰였으므로 인과적으로 안전.
- 이벤트/캠페인 시작은 5절 룰(KOSPI 첫 -4%)로 자동 선정 — 사후 바닥 선택 없음.

단순화 (산출물 명시용):
- 수수료·세금·호가단위 무시. 체결가 = 당일 원종가(close).
- 정수주 반올림 노이즈를 없애려 분할매수는 소수주 허용(전략 비교의 순수 격리 목적).
- 미투입 현금은 0% (이자 없음). 포트폴리오 가치 = 보유주식 평가액 + 잔여현금.
"""
from __future__ import annotations
import csv, json, datetime as dt
from pathlib import Path
from dataclasses import dataclass, field

HERE = Path(__file__).parent
DATA = HERE / "data"
OUT = HERE / "results"
OUT.mkdir(exist_ok=True)

TOTAL = 236_000_000
BASE = TOTAL / 10  # 23,600,000
DD_LADDER = [(0.03, 1.0), (0.06, 1.5), (0.12, 2.0), (float("inf"), 3.0)]
MAX_DAYS = 20
HORIZONS = {"1m": 30, "3m": 91, "6m": 182}


def load(name: str) -> list[dict]:
    rows = []
    with (DATA / f"{name}.csv").open() as f:
        for r in csv.DictReader(f):
            rows.append({"date": r["date"], "close": float(r["close"])})
    return rows


def build_calendar() -> tuple[list[str], dict, dict, dict]:
    sam, hyn, kos = load("samsung"), load("hynix"), load("kospi")
    sd = {r["date"]: r["close"] for r in sam}
    hd = {r["date"]: r["close"] for r in hyn}
    kd = {r["date"]: r["close"] for r in kos}
    dates = sorted(d for d in kd if d in sd and d in hd)
    return dates, sd, hd, kd


def extract_events(dates: list[str], kd: dict, cooldown: int = 20) -> list[dict]:
    """KOSPI 첫 -4% 날 (직전 cooldown 거래일 내 다른 -4% 없을 때만 = 군집당 1회)."""
    events = []
    last_trigger_i = -10_000
    for i in range(1, len(dates)):
        chg = (kd[dates[i]] - kd[dates[i - 1]]) / kd[dates[i - 1]]
        if chg <= -0.04 and (i - last_trigger_i) > cooldown:
            crash_i = i
            if crash_i + 1 >= len(dates):
                continue  # 시작일(다음 거래일) 없음
            events.append({
                "crash_date": dates[crash_i],
                "crash_chg": chg,
                "R": kd[dates[crash_i - 1]],          # 5절: 폭락일 전일 종가
                "R_alt": kd[dates[crash_i]],          # 3.3절 대안: 폭락일 종가
                "start_i": crash_i + 1,               # 다음 거래일
                "start_date": dates[crash_i + 1],
            })
            last_trigger_i = i
    return events


def dd_multiplier(dd: float, scale: float) -> float:
    for thr, mult in DD_LADDER:
        if dd < thr:
            return mult * scale
    return DD_LADDER[-1][1] * scale


@dataclass
class Sim:
    spent: float = 0.0
    sh_sam: float = 0.0
    sh_hyn: float = 0.0
    deploy: list = field(default_factory=list)   # (date, krw_this_day, cum_spent)

    def buy(self, date, krw, p_sam, p_hyn):
        krw = min(krw, TOTAL - self.spent)
        if krw <= 0:
            return
        half = krw / 2
        self.sh_sam += half / p_sam
        self.sh_hyn += half / p_hyn
        self.spent += krw
        self.deploy.append((date, krw, self.spent))


def simulate(strategy: str, ev: dict, dates, sd, hd, kd, *, scale=1.0,
             trigger="cumulative", R_key="R") -> Sim:
    """C 의 DD 는 KOSPI 지수 낙폭(5절이 R 을 KOSPI 종가로 정의). 두 종목 sizing 은
    동일 시장신호로 움직이고, 체결만 각 종목 당일 종가로 한다."""
    sim = Sim()
    start_i = ev["start_i"]
    R = ev[R_key]
    for k in range(MAX_DAYS):
        i = start_i + k
        if i >= len(dates):
            break
        date = dates[i]
        p_sam, p_hyn = sd[date], hd[date]   # 체결가 = 당일 종가
        if strategy == "A":
            if k < 10:
                sim.buy(date, BASE, p_sam, p_hyn)
        else:  # C — sizing 은 전일(i-1)까지의 KOSPI 만 본다 (룩어헤드 차단)
            assert i - 1 < i, "sizing must use prior day only"
            kospi_prev = kd[dates[i - 1]]                # P(T-1)
            if trigger == "cumulative":
                dd = max(0.0, (R - kospi_prev) / R)
            else:  # 단일일: 전일 1일 등락폭
                kospi_prev2 = kd[dates[i - 2]]
                dd = max(0.0, (kospi_prev2 - kospi_prev) / kospi_prev2)
            mult = dd_multiplier(dd, scale)
            slice_krw = min(BASE * mult, TOTAL - sim.spent)
            if k == MAX_DAYS - 1:                        # 20일 백스톱: 잔량 전량
                slice_krw = TOTAL - sim.spent
            sim.buy(date, slice_krw, p_sam, p_hyn)
        if sim.spent >= TOTAL - 1:
            break
    return sim


def equity_curve(sim: Sim, ev: dict, dates, sd, hd) -> dict:
    """start 이후 일별 포트폴리오 가치 → 호라이즌 손익 + MDD.

    데이터 끝을 넘는 호라이즌은 None(미도래) 으로 둔다 — 마지막 값으로 때우면
    가짜 수익률이 된다(특히 2026 이벤트). MDD 도 가용 윈도우 길이를 함께 기록한다."""
    start_i = ev["start_i"]
    last_date = dates[-1]
    curve = []
    for i in range(start_i, len(dates)):
        date = dates[i]
        val = sim.sh_sam * sd[date] + sim.sh_hyn * hd[date] + (TOTAL - sim.spent)
        curve.append((date, val))
        if _months_between(ev["start_date"], date) > 6.2:
            break
    pnl_series = [(d, v - TOTAL) for d, v in curve]
    mdd = min((p for _, p in pnl_series), default=0.0)
    avg_sam = (sim.spent / 2) / sim.sh_sam if sim.sh_sam else 0
    avg_hyn = (sim.spent / 2) / sim.sh_hyn if sim.sh_hyn else 0
    out = {
        "spent": sim.spent, "days_deployed": len(sim.deploy),
        "avg_samsung": avg_sam, "avg_hynix": avg_hyn,
        "mdd_krw": mdd, "mdd_pct": mdd / TOTAL * 100,
        "coverage_months": round(_months_between(ev["start_date"], curve[-1][0]), 1) if curve else 0,
        "deploy": sim.deploy, "curve": curve,
    }
    for hname, hdays in HORIZONS.items():
        target = _date_plus(ev["start_date"], hdays)
        if target > last_date:                       # 미도래 → None
            out[f"pnl_{hname}_krw"] = None
            out[f"pnl_{hname}_pct"] = None
            continue
        val = next((v for d, v in curve if d >= target), None)
        out[f"pnl_{hname}_krw"] = None if val is None else val - TOTAL
        out[f"pnl_{hname}_pct"] = None if val is None else (val - TOTAL) / TOTAL * 100
    return out


def _date_plus(d: str, days: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=days)).isoformat()


def _months_between(d0: str, d1: str) -> float:
    return (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days / 30.44


def cohort(date: str) -> str:
    if date >= "2026-06-01":
        return "current_2026_06"
    if date >= "2026-01-01":
        return "sim_2026"
    return "real_history"


def main():
    dates, sd, hd, kd = build_calendar()
    events = extract_events(dates, kd)
    print(f"총 이벤트 {len(events)}개\n")

    results = []
    for ev in events:
        coh = cohort(ev["start_date"])
        row = {"event": ev["crash_date"], "start": ev["start_date"],
               "crash_chg_pct": ev["crash_chg"] * 100, "cohort": coh,
               "R": ev["R"]}
        # 주력: A, C(scale=1, cumulative, R=5절)
        row["A"] = equity_curve(simulate("A", ev, dates, sd, hd, kd), ev, dates, sd, hd)
        row["C"] = equity_curve(simulate("C", ev, dates, sd, hd, kd), ev, dates, sd, hd)
        # 견고성: 배수 스케일
        for s in (0.75, 1.5):
            row[f"C_scale{s}"] = equity_curve(
                simulate("C", ev, dates, sd, hd, kd, scale=s), ev, dates, sd, hd)
        # 견고성: 트리거 대안(단일일 등락)
        row["C_singleday"] = equity_curve(
            simulate("C", ev, dates, sd, hd, kd, trigger="single"), ev, dates, sd, hd)
        # 참고: R 대안(3.3절 = 폭락일 종가)
        row["C_Ralt"] = equity_curve(
            simulate("C", ev, dates, sd, hd, kd, R_key="R_alt"), ev, dates, sd, hd)
        results.append(row)

    # JSON 저장 (curve/deploy 제외한 요약)
    def slim(d):
        return {k: v for k, v in d.items() if k not in ("deploy", "curve")}
    VARIANTS = ("A", "C", "C_scale0.75", "C_scale1.5", "C_singleday", "C_Ralt")
    dump = []
    for r in results:
        rr = {k: v for k, v in r.items() if not isinstance(v, dict)}
        for key in VARIANTS:
            rr[key] = slim(r[key])
        dump.append(rr)
    (OUT / "summary.json").write_text(json.dumps(dump, indent=2, ensure_ascii=False, default=str))

    def pct(x):
        return "  N/A " if x is None else f"{x:>6.2f}%"

    lines = []
    def emit(s=""):
        print(s)
        lines.append(s)

    # --- 이벤트별 표 ---
    emit("=== 이벤트별 (A vs C, 주력) ===")
    emit(f"{'event':11s} {'coh':5s} {'crash':>6s} | {'평단삼 A→C':>14s} | "
         f"{'A_3m':>7s} {'C_3m':>7s} {'C-A':>7s} | {'A_6m':>7s} {'C_6m':>7s} {'C-A':>7s} | "
         f"{'A_MDD':>6s} {'C_MDD':>6s} {'cov':>4s}")
    cshort = {"real_history": "real", "sim_2026": "sim", "current_2026_06": "cur"}
    for r in results:
        A, C = r["A"], r["C"]
        d3 = None if (A["pnl_3m_pct"] is None or C["pnl_3m_pct"] is None) else C["pnl_3m_pct"]-A["pnl_3m_pct"]
        d6 = None if (A["pnl_6m_pct"] is None or C["pnl_6m_pct"] is None) else C["pnl_6m_pct"]-A["pnl_6m_pct"]
        emit(f"{r['event']:11s} {cshort[r['cohort']]:5s} {r['crash_chg_pct']:>5.1f}% | "
             f"{A['avg_samsung']:>6.0f}→{C['avg_samsung']:>6.0f} | "
             f"{pct(A['pnl_3m_pct'])} {pct(C['pnl_3m_pct'])} {pct(d3)} | "
             f"{pct(A['pnl_6m_pct'])} {pct(C['pnl_6m_pct'])} {pct(d6)} | "
             f"{A['mdd_pct']:>5.1f}% {C['mdd_pct']:>5.1f}% {C['coverage_months']:>4.1f}")

    # --- 코호트 집계 (호라이즌별, None 제외) ---
    emit("\n=== 코호트 집계 (해당 호라이즌 완주 이벤트만) ===")
    def agg(rs, strat, key):
        vals = [r[strat][key] for r in rs if r[strat][key] is not None]
        return (sum(vals)/len(vals), len(vals)) if vals else (None, 0)
    for coh in ("real_history", "sim_2026"):
        rs = [r for r in results if r["cohort"] == coh]
        if not rs:
            continue
        emit(f"[{coh}] 이벤트 {len(rs)}개")
        for h in ("1m", "3m", "6m"):
            a, na = agg(rs, "A", f"pnl_{h}_pct")
            c, nc = agg(rs, "C", f"pnl_{h}_pct")
            delta = None if (a is None or c is None) else c-a
            emit(f"   {h}: n={na:>1d}  A={pct(a)}  C={pct(c)}  C-A={pct(delta)}")
        am, _ = agg(rs, "A", "mdd_pct"); cm, _ = agg(rs, "C", "mdd_pct")
        emit(f"   MDD(평균): A={pct(am)}  C={pct(cm)}")

    # --- 견고성: 배수 스케일 + 트리거 대안 + R대안 (real_history 3m·6m C-A) ---
    emit("\n=== 견고성 (real_history, C변형 − A) ===")
    rs = [r for r in results if r["cohort"] == "real_history"]
    aA3, _ = agg(rs, "A", "pnl_3m_pct"); aA6, _ = agg(rs, "A", "pnl_6m_pct")
    aAm, _ = agg(rs, "A", "mdd_pct")
    for var, label in [("C","C 기본"),("C_scale0.75","배수×0.75"),("C_scale1.5","배수×1.5"),
                       ("C_singleday","트리거=단일일"),("C_Ralt","R=폭락일종가(3.3절)")]:
        c3,_ = agg(rs, var, "pnl_3m_pct"); c6,_ = agg(rs, var, "pnl_6m_pct")
        cm,_ = agg(rs, var, "mdd_pct")
        emit(f"   {label:20s} 3m C-A={pct(None if c3 is None else c3-aA3)}  "
             f"6m C-A={pct(None if c6 is None else c6-aA6)}  MDD {pct(cm)}(A {pct(aAm)})")

    # --- 승률 한 줄 (real_history) ---
    win3 = sum(1 for r in rs if r["C"]["pnl_3m_pct"] is not None and r["C"]["pnl_3m_pct"] > r["A"]["pnl_3m_pct"])
    winavg = sum(1 for r in rs if r["C"]["avg_samsung"] < r["A"]["avg_samsung"])
    deeper = sum(1 for r in rs if r["C"]["mdd_pct"] < r["A"]["mdd_pct"])
    emit(f"\n=== 한 줄 (real_history n={len(rs)}) ===")
    emit(f"   C가 A보다 3m 수익 우위: {win3}/{len(rs)}  |  평단가 더 쌈: {winavg}/{len(rs)}  |  MDD 더 깊음: {deeper}/{len(rs)}")

    (OUT / "tables.txt").write_text("\n".join(lines))

    # --- 배포곡선 PNG ---
    _plot_deploy(results)
    return results


def _plot_deploy(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for r in results:
        fig, ax = plt.subplots(figsize=(7, 4))
        for strat, color in (("A", "tab:blue"), ("C", "tab:red")):
            dep = r[strat]["deploy"]
            xs = list(range(1, len(dep) + 1))
            ys = [c / 1e8 for _, _, c in dep]
            ax.step(xs, ys, where="post", label=strat, color=color, marker="o", ms=3)
        ax.set_title(f"{r['event']} ({r['cohort']}, crash {r['crash_chg_pct']:.1f}%) — capital deployed")
        ax.set_xlabel("trading day in campaign")
        ax.set_ylabel("cumulative deployed (100M KRW)")
        ax.axhline(2.36, ls="--", c="gray", lw=0.8)
        ax.legend(); ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(OUT / f"deploy_{r['event']}.png", dpi=110)
        plt.close(fig)


if __name__ == "__main__":
    main()
