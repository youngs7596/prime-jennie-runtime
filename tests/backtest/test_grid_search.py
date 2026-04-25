"""``scripts.grid_search`` driver smoke 테스트.

Process pool 은 windows/wsl 환경 차이로 CI 안정성이 떨어지므로 기본은
``workers=1`` (in-process) 로 검증. 실제 병렬 경로는 별도 통합 테스트로 분리.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from prime_jennie_runtime.backtest import DailyBar
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    SizeSection,
    TimeStopRule,
)
from scripts import grid_search


def _mk_sheet_dict() -> dict:
    generated_at = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    valid_until = generated_at.replace(hour=15, minute=20)
    sheet = PositionSheet(
        sheet_id="ps_20260504_090000_a1b2",
        schema_version="1.1",
        generated_at=generated_at,
        valid_until=valid_until,
        ticker="005930",
        side="long",
        strategy_tag="GAP_UP_REBOUND",
        size=SizeSection(
            base_pct=0.05,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.05,
            max_notional_krw=5_000_000,
        ),
        entry=EntrySection(
            trigger="market",
            price=None,
            valid_until=valid_until,
            conditions=[],
        ),
        exit=ExitSection(rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="hold_days", value=5)]),
        provenance=ProvenanceSection(
            scout_run_id="scout_test",
            scout_code_hash="sha256:test",
            scout_hypothesis="test",
            macro_state_snapshot=MacroStateSnapshot(
                gate="open", size_multiplier=1.0, gate_run_id="macro_test"
            ),
            strategy_policy_version="test",
            generated_by="test",
        ),
    )
    return sheet.model_dump(mode="json")


def _bars() -> list[DailyBar]:
    bars: list[DailyBar] = []
    d = date(2026, 5, 4)
    ohlcv = [(10_000, 10_100, 9_900, 10_000)] * 8
    for o, h, lo, c in ohlcv:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        bars.append(
            DailyBar(
                ticker="005930",
                price_date=d,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=1_000_000,
            )
        )
        d += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_split_path_handles_dot_and_index():
    assert grid_search._split_path("exit.rules[0].pct") == ["exit", "rules", 0, "pct"]
    assert grid_search._split_path("size.base_pct") == ["size", "base_pct"]
    assert grid_search._split_path("a[2][3]") == ["a", 2, 3]


def test_set_path_overlays_nested_value():
    obj = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    grid_search._set_path(obj, "a.b[1].c", 99)
    assert obj["a"]["b"][1]["c"] == 99


def test_set_path_missing_key_raises():
    with pytest.raises(KeyError):
        grid_search._set_path({"a": 1}, "b.c", 0)


# ---------------------------------------------------------------------------
# Grid expansion
# ---------------------------------------------------------------------------


def test_expand_grid_cartesian():
    grid = {"x": [1, 2], "y": [10, 20]}
    combos = grid_search.expand_grid(grid)
    assert len(combos) == 4
    assert {"x": 1, "y": 10} in combos
    assert {"x": 2, "y": 20} in combos


def test_combo_id_deterministic_and_short():
    cid = grid_search.combo_id({"a": 1, "b": 2})
    assert len(cid) == 8
    # 키 순서 무관
    assert grid_search.combo_id({"b": 2, "a": 1}) == cid


# ---------------------------------------------------------------------------
# Apply params end-to-end
# ---------------------------------------------------------------------------


def test_apply_params_overlays_fixed_sl_pct():
    template = _mk_sheet_dict()
    out = grid_search.apply_params(template, {"exit.rules[0].pct": 0.07})
    assert out["exit"]["rules"][0]["pct"] == 0.07
    # 원본 변경 없음
    assert template["exit"]["rules"][0]["pct"] == 0.05


# ---------------------------------------------------------------------------
# run_grid happy path (workers=1)
# ---------------------------------------------------------------------------


def test_run_grid_happy_path_in_process():
    template = _mk_sheet_dict()
    bars = _bars()
    grid = {
        "exit.rules[0].pct": [0.03, 0.05],
        "exit.rules[1].value": [3, 5],
    }

    aggregated = grid_search.run_grid(
        sheet_template=template,
        param_grid=grid,
        bars=bars,
        capital_krw=1_000_000,
        workers=1,
    )

    assert aggregated["meta"]["n_combos"] == 4
    assert aggregated["meta"]["bars_n"] == len(bars)
    assert aggregated["meta"]["ticker"] == "005930"
    assert len(aggregated["results"]) == 4
    for cid, entry in aggregated["results"].items():
        assert len(cid) == 8
        assert entry["ok"] is True
        assert "params" in entry
        assert "summary" in entry
        assert "sheet_result" in entry
        assert entry["summary"]["n_total"] == 1


# ---------------------------------------------------------------------------
# run_grid 한 조합이 schema reject 되어도 다른 조합은 살아남는다
# ---------------------------------------------------------------------------


def test_run_grid_isolates_bad_combo():
    template = _mk_sheet_dict()
    bars = _bars()
    # 0.0 은 fixed_sl pct gt=0 으로 Pydantic Field validation 실패 → 격리되어야 함
    grid = {"exit.rules[0].pct": [0.0, 0.05]}

    aggregated = grid_search.run_grid(
        sheet_template=template,
        param_grid=grid,
        bars=bars,
        capital_krw=1_000_000,
        workers=1,
    )

    assert aggregated["meta"]["n_combos"] == 2
    statuses = [entry["ok"] for entry in aggregated["results"].values()]
    assert True in statuses
    assert False in statuses
    # 실패 entry 는 error 메시지를 갖는다
    bad = next(e for e in aggregated["results"].values() if not e["ok"])
    assert "error" in bad


# ---------------------------------------------------------------------------
# CLI end-to-end (workers=1)
# ---------------------------------------------------------------------------


def test_cli_main_writes_aggregated_json(tmp_path: Path):
    sheet_path = tmp_path / "sheet.json"
    sheet_path.write_text(json.dumps(_mk_sheet_dict()), encoding="utf-8")

    grid_path = tmp_path / "grid.yaml"
    grid_path.write_text(
        yaml.safe_dump({"exit.rules[0].pct": [0.03, 0.05]}),
        encoding="utf-8",
    )

    bars = _bars()
    bars_path = tmp_path / "bars.json"
    bars_path.write_text(
        json.dumps(
            [
                {
                    "ticker": b.ticker,
                    "price_date": b.price_date.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
            ]
        ),
        encoding="utf-8",
    )

    out_path = tmp_path / "out.json"

    rc = grid_search.main(
        [
            "--sheet-template",
            str(sheet_path),
            "--param-grid",
            str(grid_path),
            "--bars",
            str(bars_path),
            "--capital",
            "1000000",
            "--workers",
            "1",
            "--output",
            str(out_path),
            "--log-level",
            "WARNING",
        ]
    )

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["meta"]["n_combos"] == 2
    assert len(payload["results"]) == 2
    for entry in payload["results"].values():
        assert entry["ok"] is True


# ---------------------------------------------------------------------------
# I/O loaders 검증
# ---------------------------------------------------------------------------


def test_load_param_grid_rejects_empty(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        grid_search.load_param_grid(p)


def test_load_param_grid_rejects_non_list_values(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("foo: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        grid_search.load_param_grid(p)
