"""새 점수(total3)에 진입·이탈 문턱 후보를 대 보고 현행 66/62 규모와 비교한다.

인자: CSV 폴더(scores3.csv — 같은 날 recompute 스크립트 산출).
"""

import sys
from importlib import import_module
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
_replay = import_module("2026-10-09-scorer-v3-threshold-replay")
load, replay = _replay.load, _replay.replay
D = sys.argv[1]
sc, runs = load(D)
mo = runs[(runs.gate == "open") & (runs.gen.dt.strftime("%H:%M") == "08:30")]
op = runs[runs.gate == "open"]


def stats(sel, label):
    n = np.array([len(sel[r]) for r in mo.index])
    c = np.array([sel[r + "#carry"] for r in mo.index])
    cap = sum(sel[r + "#passed"] > 20 for r in op.index)
    # 회차별 주별 평균
    wk = pd.Series(n, index=pd.to_datetime(mo.d)).resample("W").mean().round(1).tolist()
    head = f"{label:18s} 아침선정 {n.mean():5.1f} 진입권 {(n - c).mean():5.1f} 이월 {c.mean():4.1f}"
    print(f"{head} 0건아침 {(n == 0).sum():2d} 상한걸림 {cap:3d}/{len(op)}  주별 {wk}")


base = replay(sc, runs, "total", lambda d: (66.0, 62.0))
stats(base, "현행 @2 66/62")
for e, x in [(70, 66), (71, 67), (72, 68), (72.5, 68.5), (72.5, 69), (73, 69), (74, 70)]:
    stats(replay(sc, runs, "total3", lambda d, e=e, x=x: (e, x)), f"@3 {e}/{x}")
