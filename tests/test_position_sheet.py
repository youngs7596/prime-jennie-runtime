"""position_sheet/schema.py 테스트 — POSITION_SHEET_SPEC v1.1 검증."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.position_sheet.schema import (
    KST,
    PositionSheet,
    ThesisCondition,
    ThesisSpec,
)


def _base_sheet(**overrides) -> dict:
    """유효한 기본 시트 dict. overrides로 필드 덮어쓰기."""
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    defaults = {
        "sheet_id": "ps_20260416_005930_a3f2",
        "schema_version": "1.1",
        "generated_at": now,
        "valid_until": now + timedelta(hours=6),
        "ticker": "005930",
        "side": "long",
        "strategy_tag": "GAP_UP_REBOUND",
        "size": {
            "base_pct": 0.05,
            "macro_multiplier": 0.7,
            "risk_multiplier": 1.0,
            "final_pct": 0.035,
            "max_notional_krw": 5_000_000,
        },
        "entry": {
            "trigger": "limit",
            "price": 71200,
            "valid_until": now + timedelta(hours=1),
            "conditions": [],
        },
        "exit": {
            "rules": [
                {"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03},
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ],
            "priority": "first_match",
        },
        "provenance": {
            "scout_run_id": "scout_20260416_0900",
            "scout_code_hash": "sha256:abc123",
            "scout_hypothesis": "반도체 섹터 모멘텀 재점화",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 0.7,
                "gate_run_id": "macro_20260416_0800",
            },
            "news_score_at_generation": 0.42,
            "strategy_policy_version": "v3.0.1",
            "generated_by": "prime-jennie-runtime@v3.0.1",
        },
    }
    defaults.update(overrides)
    return defaults


# T01: 정상 시트 발행
def test_valid_sheet():
    sheet = PositionSheet(**_base_sheet())
    assert sheet.sheet_id == "ps_20260416_005930_a3f2"
    assert sheet.schema_version == "1.1"


# T02: fixed_sl 누락
def test_missing_fixed_sl():
    data = _base_sheet()
    data["exit"]["rules"] = [
        {"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03},
        {"type": "time_stop", "mode": "eod"},
    ]
    with pytest.raises(ValueError, match="fixed_sl"):
        PositionSheet(**data)


# T03: time_stop 누락
def test_missing_time_stop():
    data = _base_sheet()
    data["exit"]["rules"] = [{"type": "fixed_sl", "pct": 0.05}]
    with pytest.raises(ValueError, match="time_stop"):
        PositionSheet(**data)


# T04: final_pct != base*macro*risk
def test_size_mismatch():
    data = _base_sheet()
    data["size"]["final_pct"] = 0.099  # 틀린 값
    with pytest.raises(ValueError, match="final_pct"):
        PositionSheet(**data)


# T05: valid_until < generated_at
def test_invalid_time_order():
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    data = _base_sheet(valid_until=now - timedelta(hours=1))
    with pytest.raises(ValueError, match="before"):
        PositionSheet(**data)


# T07: strategy_tag RSI_REBOUND (폐기)
def test_deprecated_strategy_tag():
    data = _base_sheet(strategy_tag="RSI_REBOUND")
    with pytest.raises(ValueError, match="deprecated"):
        PositionSheet(**data)


# T08: scale_out portion 합 초과
def test_scale_out_portion_exceeded():
    data = _base_sheet()
    data["exit"]["rules"] = [
        {"type": "scale_out", "levels": [[0.03, 0.6], [0.05, 0.6]]},  # 합 1.2
        {"type": "fixed_sl", "pct": 0.05},
        {"type": "time_stop", "mode": "eod"},
    ]
    with pytest.raises(ValueError, match="scale_out portion"):
        PositionSheet(**data)


# T21: recovery_exit — 음수~0 임계 수용, 양수·과대 음수 거부 (SPEC §5.2.10)
def test_recovery_exit_accepted():
    data = _base_sheet()
    data["exit"]["rules"] = [
        {"type": "recovery_exit", "pct": -0.01},
        {"type": "fixed_sl", "pct": 0.05},
        {"type": "time_stop", "mode": "eod"},
    ]
    sheet = PositionSheet(**data)
    assert {r.type for r in sheet.exit.rules} >= {"recovery_exit", "fixed_sl", "time_stop"}


def test_recovery_exit_rejects_positive_pct():
    # 양수 목표는 fixed_tp 의 역할 — recovery_exit 은 0 이하만
    data = _base_sheet()
    data["exit"]["rules"] = [
        {"type": "recovery_exit", "pct": 0.02},
        {"type": "fixed_sl", "pct": 0.05},
        {"type": "time_stop", "mode": "eod"},
    ]
    with pytest.raises(ValueError):
        PositionSheet(**data)


def test_recovery_exit_rejects_below_minus_10pct():
    data = _base_sheet()
    data["exit"]["rules"] = [
        {"type": "recovery_exit", "pct": -0.15},
        {"type": "fixed_sl", "pct": 0.05},
        {"type": "time_stop", "mode": "eod"},
    ]
    with pytest.raises(ValueError):
        PositionSheet(**data)


# T11: entry.valid_until > sheet.valid_until
def test_entry_valid_until_exceeds_sheet():
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    data = _base_sheet()
    data["entry"]["valid_until"] = now + timedelta(hours=10)  # sheet는 +6h
    with pytest.raises(ValueError, match="entry.valid_until"):
        PositionSheet(**data)


# T12: final_pct < MIN_POSITION_PCT
def test_min_position_pct():
    data = _base_sheet()
    data["size"] = {
        "base_pct": 0.01,
        "macro_multiplier": 0.3,
        "risk_multiplier": 1.0,
        "final_pct": 0.003,
        "max_notional_krw": 1_000_000,
    }
    with pytest.raises(ValueError, match="MIN"):
        PositionSheet(**data)


# sheet_id 포맷 불일치
def test_invalid_sheet_id():
    data = _base_sheet(sheet_id="bad_id")
    with pytest.raises(ValueError, match="sheet_id"):
        PositionSheet(**data)


# market trigger with price
def test_market_trigger_with_price():
    data = _base_sheet()
    data["entry"]["trigger"] = "market"
    data["entry"]["price"] = 71200
    with pytest.raises(ValueError, match="market trigger"):
        PositionSheet(**data)


# 직렬화/역직렬화 라운드트립
def test_json_roundtrip():
    sheet = PositionSheet(**_base_sheet())
    json_str = sheet.model_dump_json()
    restored = PositionSheet.model_validate_json(json_str)
    assert restored.sheet_id == sheet.sheet_id
    assert restored.size.final_pct == sheet.size.final_pct


# =====================================================================
# G6 thesis-aware exit Phase A (2026-05-17) — ThesisSpec / ThesisCondition schema
# design `.ai/designs/2026-05-17-g6-thesis-aware-exit.md` §4
# =====================================================================


def test_thesis_condition_catalog_8_types_accepted():
    """catalog 8종 type 모두 ThesisCondition 으로 인스턴스화 가능."""
    types = [
        "kospi_gate",
        "kospi_change_pct_above",
        "sector_momentum_above",
        "no_risk_event_high",
        "earnings_event_window",
        "rsi_below",
        "price_above_breakout",
        "r20d_above_threshold",
    ]
    for t in types:
        c = ThesisCondition(type=t, params={"k": 1})
        assert c.type == t
        assert c.params == {"k": 1}


def test_thesis_condition_unknown_type_rejected():
    """catalog 외 type 은 Literal 검증으로 거절."""
    with pytest.raises(ValueError):
        ThesisCondition(type="custom_type", params={})


def test_thesis_spec_defaults_to_empty():
    """ThesisSpec 모든 필드 default — Phase A 호환."""
    spec = ThesisSpec()
    assert spec.natural_language == ""
    assert spec.conditions == []
    assert spec.critical_conditions == []


def test_thesis_spec_roundtrip():
    """model_dump / validate roundtrip — provenance_json 영속 시 사용 패턴."""
    original = ThesisSpec(
        natural_language="hypothesis",
        conditions=[
            ThesisCondition(type="kospi_gate", params={"required": "open"}),
            ThesisCondition(type="r20d_above_threshold", params={"min_pct": 0.0}),
        ],
        critical_conditions=[0],
    )
    dumped = original.model_dump(mode="json")
    restored = ThesisSpec.model_validate(dumped)
    assert restored == original
    assert restored.conditions[0].type == "kospi_gate"
    assert restored.critical_conditions == [0]


def test_position_sheet_with_thesis_spec_roundtrip():
    """PositionSheet 의 provenance.thesis_spec 직렬화/역직렬화."""
    data = _base_sheet()
    data["provenance"]["thesis_spec"] = {
        "natural_language": "test",
        "conditions": [
            {"type": "kospi_gate", "params": {"required": "open"}},
        ],
        "critical_conditions": [0],
    }
    sheet = PositionSheet(**data)
    assert sheet.provenance.thesis_spec is not None
    assert sheet.provenance.thesis_spec.conditions[0].type == "kospi_gate"

    json_str = sheet.model_dump_json()
    restored = PositionSheet.model_validate_json(json_str)
    assert restored.provenance.thesis_spec is not None
    assert restored.provenance.thesis_spec.critical_conditions == [0]


def test_position_sheet_thesis_spec_default_none():
    """기존 sheet (thesis_spec 미포함) 도 PositionSheet 생성 가능 — 호환."""
    sheet = PositionSheet(**_base_sheet())
    assert sheet.provenance.thesis_spec is None
