"""CouncilRunRecord — dataclass 기본 동작."""

from __future__ import annotations

from datetime import date, datetime

from prime_jennie_runtime.council_logging import CouncilRunRecord, CouncilStepOutput


def _record(**overrides) -> CouncilRunRecord:
    defaults = dict(
        macro_run_id="macro_20260417_0800",
        generated_at=datetime(2026, 4, 17, 8, 0, 0),
        trigger_reason="scheduled_0800",
        gate="open",
        size_multiplier=1.0,
    )
    defaults.update(overrides)
    return CouncilRunRecord(**defaults)


def test_step_by_name_returns_matching_step():
    steps = [
        CouncilStepOutput(name="strategist", output={"x": 1}),
        CouncilStepOutput(name="chief_judge", output={"y": 2}),
    ]
    rec = _record(steps=steps)
    assert rec.step_by_name("chief_judge").output == {"y": 2}
    assert rec.step_by_name("nonexistent") is None


def test_total_cost_prefers_record_level_cost():
    rec = _record(
        cost_usd=0.5,
        steps=[
            CouncilStepOutput(name="a", cost_usd=0.1),
            CouncilStepOutput(name="b", cost_usd=0.2),
        ],
    )
    # record-level cost wins
    assert rec.total_cost_usd() == 0.5


def test_total_cost_falls_back_to_step_sum():
    rec = _record(
        cost_usd=None,
        steps=[
            CouncilStepOutput(name="a", cost_usd=0.1),
            CouncilStepOutput(name="b", cost_usd=0.2),
            CouncilStepOutput(name="c", cost_usd=None),
        ],
    )
    assert abs(rec.total_cost_usd() - 0.3) < 1e-9


def test_record_defaults_are_safe():
    rec = _record()
    assert rec.steps == []
    assert rec.top_risks == []
    assert rec.strategies_to_favor == []
    assert rec.success is True
    assert rec.error == ""
    assert rec.target_date is None


def test_record_with_target_date_preserved():
    rec = _record(target_date=date(2026, 4, 17))
    assert rec.target_date == date(2026, 4, 17)
