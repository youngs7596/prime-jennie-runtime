"""Grid search driver — Phase 3 meta 레이어가 후보 파라미터 공간을 휩쓸 때 호출.

Phase 3 backtest AGENTS.md 에 따르면 "grid search 2,800 조합" 자체는 meta
레이어 책임이지만, **드라이버 스크립트** 는 runtime 측에 둔다 (단일 백테스트
엔진/시트 스키마 의존성을 한 곳에서 관리). meta 의 Eval Pipeline 이 이
스크립트를 호출하거나 동일 로직을 import 해서 쓴다.

입력
----
* ``--sheet-template`` — schema v1.1 PositionSheet JSON. param-grid 가 일부 필드를
  overlay 한다. dotted-path 로 ``exit.rules[0].pct`` 같은 nested 필드 접근.
* ``--param-grid`` — YAML. 각 key 는 sheet 의 dotted path, value 는 후보 list.
  Cartesian product 로 조합 생성. 예::

      "exit.rules[0].pct": [0.03, 0.05, 0.07]
      "exit.rules[1].value": [3, 5, 10]

* ``--bars`` — JSON 파일. ``[{"price_date": "2026-05-04", "open": 10000, ...}, ...]``
  형태. 모든 조합이 동일 bar 시퀀스 위에서 평가된다.
* ``--capital`` — KRW. 모든 조합 동일.
* ``--entry-index`` — bars 내 entry 후보일 index. 기본 0.

출력
----
JSON: ``{"meta": {...}, "results": {combo_id: {"params": {...}, "summary": {...},
"sheet_result": {...}}}}``. ``combo_id`` 는 param 값들의 deterministic hash 8자.

동시성
------
``ProcessPoolExecutor`` 로 조합 분배 — ``simulate_sheet`` 는 CPU-bound 이고
조합간 독립이라 GIL 회피로 거의 선형 스케일.

사용 예 ::

    python -m scripts.grid_search \\
        --sheet-template fixtures/sheet.json \\
        --param-grid fixtures/grid.yaml \\
        --bars fixtures/bars_005930.json \\
        --capital 5000000 \\
        --output reports/grid-2026-04-25.json
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import itertools
import json
import logging
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from prime_jennie_runtime.backtest import (
    BacktestConfig,
    DailyBar,
    SheetBacktestResult,
    simulate_sheet,
    summarize,
)
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger("grid_search")

# dotted path 토큰: ``foo`` 또는 ``foo[3]``
_PATH_TOKEN_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)((?:\[\d+\])*)")
_INDEX_RE = re.compile(r"\[(\d+)\]")


# ---------------------------------------------------------------------------
# dotted path helpers
# ---------------------------------------------------------------------------


def _split_path(path: str) -> list[str | int]:
    """``"exit.rules[0].pct"`` → ``["exit", "rules", 0, "pct"]``."""
    tokens: list[str | int] = []
    for part in path.split("."):
        m = _PATH_TOKEN_RE.fullmatch(part)
        if not m:
            raise ValueError(f"invalid path segment: {part!r}")
        name, indices = m.group(1), m.group(2)
        tokens.append(name)
        for idx_match in _INDEX_RE.finditer(indices):
            tokens.append(int(idx_match.group(1)))
    return tokens


def _set_path(obj: dict[str, Any] | list[Any], path: str, value: Any) -> None:
    """dict/list 트리에 dotted path 로 값 주입. 존재하지 않는 path 는 KeyError."""
    tokens = _split_path(path)
    cur: Any = obj
    for tok in tokens[:-1]:
        if isinstance(tok, int):
            if not isinstance(cur, list) or tok >= len(cur):
                raise KeyError(f"index {tok} out of range at path {path!r}")
            cur = cur[tok]
        else:
            if not isinstance(cur, dict) or tok not in cur:
                raise KeyError(f"key {tok!r} missing at path {path!r}")
            cur = cur[tok]

    last = tokens[-1]
    if isinstance(last, int):
        if not isinstance(cur, list) or last >= len(cur):
            raise KeyError(f"final index {last} out of range at path {path!r}")
        cur[last] = value
    else:
        if not isinstance(cur, dict):
            raise KeyError(f"final key {last!r} target is not a dict at path {path!r}")
        cur[last] = value


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise ValueError(f"cannot parse date: {value!r}")


def load_bars(path: Path) -> list[DailyBar]:
    """JSON 파일에서 ``DailyBar`` 시퀀스 로드. 오름차순 보장."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"bars file must be a JSON list: {path}")
    bars: list[DailyBar] = []
    for entry in raw:
        bars.append(
            DailyBar(
                ticker=str(entry["ticker"]),
                price_date=_parse_date(entry["price_date"]),
                open=float(entry["open"]),
                high=float(entry["high"]),
                low=float(entry["low"]),
                close=float(entry["close"]),
                volume=int(entry.get("volume", 0)),
            )
        )
    bars.sort(key=lambda b: b.price_date)
    return bars


def load_sheet_template(path: Path) -> dict[str, Any]:
    """JSON 파일 → 그대로 dict. param 적용 후 ``PositionSheet.model_validate`` 통과해야 한다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"sheet template must be a JSON object: {path}")
    return data


def load_param_grid(path: Path) -> dict[str, list[Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"param grid must be a non-empty mapping: {path}")
    grid: dict[str, list[Any]] = {}
    for key, values in raw.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"param grid value for {key!r} must be a non-empty list")
        grid[str(key)] = list(values)
    return grid


# ---------------------------------------------------------------------------
# Grid expansion
# ---------------------------------------------------------------------------


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product → list of param-set dicts. 빈 grid 면 [{}] 1회."""
    if not grid:
        return [{}]
    keys = list(grid.keys())
    value_lists = [grid[k] for k in keys]
    combos: list[dict[str, Any]] = []
    for values in itertools.product(*value_lists):
        combos.append(dict(zip(keys, values, strict=True)))
    return combos


def combo_id(params: dict[str, Any]) -> str:
    """Deterministic 8-char hash. 동일 params 는 동일 id."""
    payload = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:8]


def apply_params(
    template: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    """template 깊은 복사 후 params 의 dotted-path 들을 overlay."""
    sheet = copy.deepcopy(template)
    for path, value in params.items():
        _set_path(sheet, path, value)
    return sheet


# ---------------------------------------------------------------------------
# Single combo execution (process pool entry point)
# ---------------------------------------------------------------------------


def _result_to_dict(result: SheetBacktestResult) -> dict[str, Any]:
    d = dataclasses.asdict(result)
    for k in ("entry_ts", "exit_ts"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for t in d.get("trades", []):
        if t.get("ts") is not None:
            t["ts"] = t["ts"].isoformat()
    return d


def _summary_to_dict(results: list[SheetBacktestResult]) -> dict[str, Any]:
    summary = summarize(results)
    return dataclasses.asdict(summary)


def run_combo(
    sheet_dict: dict[str, Any],
    bars: list[DailyBar],
    capital_krw: float,
    entry_index: int,
    config: BacktestConfig,
) -> SheetBacktestResult:
    """단일 조합 — sheet validate → simulate. ValidationError 등은 caller 에서 처리."""
    sheet = PositionSheet.model_validate(sheet_dict)
    return simulate_sheet(
        sheet,
        bars,
        entry_index=entry_index,
        capital_krw=capital_krw,
        config=config,
    )


def _worker(payload: dict[str, Any]) -> dict[str, Any]:
    """ProcessPool 워커 — 직렬화 가능한 dict 만 주고받는다."""
    cid: str = payload["combo_id"]
    params: dict[str, Any] = payload["params"]
    sheet_dict: dict[str, Any] = payload["sheet"]
    capital: float = payload["capital_krw"]
    entry_index: int = payload["entry_index"]
    config = BacktestConfig(**payload["config"])
    bars = [DailyBar(**b) for b in payload["bars"]]

    try:
        result = run_combo(
            sheet_dict=sheet_dict,
            bars=bars,
            capital_krw=capital,
            entry_index=entry_index,
            config=config,
        )
    except Exception as e:  # noqa: BLE001 — 조합 1개 실패가 전체를 멈추지 않게 격리
        return {
            "combo_id": cid,
            "params": params,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }

    return {
        "combo_id": cid,
        "params": params,
        "ok": True,
        "sheet_result": _result_to_dict(result),
        "summary": _summary_to_dict([result]),
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def _bar_to_dict(bar: DailyBar) -> dict[str, Any]:
    return {
        "ticker": bar.ticker,
        "price_date": bar.price_date,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def run_grid(
    *,
    sheet_template: dict[str, Any],
    param_grid: dict[str, list[Any]],
    bars: list[DailyBar],
    capital_krw: float,
    entry_index: int = 0,
    config: BacktestConfig | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    """Grid 전개 → 병렬 실행 → aggregated dict 반환.

    workers <= 1 이면 in-process 순차 실행 (테스트/디버깅 친화). 그 외엔
    ``ProcessPoolExecutor`` 로 분배.
    """
    cfg = config or BacktestConfig()
    combos = expand_grid(param_grid)

    payloads: list[dict[str, Any]] = []
    for params in combos:
        sheet_dict = apply_params(sheet_template, params)
        payloads.append(
            {
                "combo_id": combo_id(params),
                "params": params,
                "sheet": sheet_dict,
                "bars": [_bar_to_dict(b) for b in bars],
                "capital_krw": capital_krw,
                "entry_index": entry_index,
                "config": dataclasses.asdict(cfg),
            }
        )

    if workers <= 1:
        outputs = [_worker(p) for p in payloads]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            outputs = list(ex.map(_worker, payloads))

    results: dict[str, dict[str, Any]] = {}
    for out in outputs:
        results[out["combo_id"]] = {k: v for k, v in out.items() if k != "combo_id"}

    return {
        "meta": {
            "n_combos": len(combos),
            "param_keys": list(param_grid.keys()),
            "capital_krw": capital_krw,
            "entry_index": entry_index,
            "config": dataclasses.asdict(cfg),
            "bars_n": len(bars),
            "ticker": bars[0].ticker if bars else None,
        },
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sheet-template", required=True, type=Path)
    p.add_argument("--param-grid", required=True, type=Path)
    p.add_argument("--bars", required=True, type=Path)
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--entry-index", type=int, default=0)
    p.add_argument("--buy-fee", type=float, default=0.015)
    p.add_argument("--sell-fee", type=float, default=0.195)
    p.add_argument("--slippage", type=float, default=0.1)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    sheet_template = load_sheet_template(args.sheet_template)
    grid = load_param_grid(args.param_grid)
    bars = load_bars(args.bars)
    cfg = BacktestConfig(
        buy_fee_pct=args.buy_fee,
        sell_fee_pct=args.sell_fee,
        slippage_pct=args.slippage,
    )

    logger.info(
        "grid: keys=%s n_combos=%d bars=%d ticker=%s",
        list(grid.keys()),
        len(expand_grid(grid)),
        len(bars),
        bars[0].ticker if bars else "?",
    )

    aggregated = run_grid(
        sheet_template=sheet_template,
        param_grid=grid,
        bars=bars,
        capital_krw=args.capital,
        entry_index=args.entry_index,
        config=cfg,
        workers=args.workers,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(aggregated, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("wrote %s (n_combos=%d)", args.output, aggregated["meta"]["n_combos"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
