"""replay.build_replay / replay_council_run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from prime_jennie_runtime.council_logging import (
    CouncilRunRecord,
    CouncilStepOutput,
    build_replay,
    replay_council_run,
)


def _record() -> CouncilRunRecord:
    return CouncilRunRecord(
        macro_run_id="macro_20260417_0800",
        generated_at=datetime(2026, 4, 17, 8, 0),
        trigger_reason="scheduled_0800",
        gate="open",
        size_multiplier=1.0,
        target_date=date(2026, 4, 17),
        steps=[
            CouncilStepOutput(name="strategist", output={"sentiment_score": 72}),
            CouncilStepOutput(name="chief_judge", output={"council_consensus": "agree"}),
        ],
        input_briefing_text="brief",
        input_global_snapshot={"vix": 15.2},
        input_political_news=["n1"],
    )


def test_build_replay_exposes_input_context_and_step_outputs():
    rep = build_replay(_record())
    assert rep.input_context["briefing_text"] == "brief"
    assert rep.input_context["global_snapshot"] == {"vix": 15.2}
    assert rep.input_context["political_news"] == ["n1"]
    assert rep.input_context["target_date"] == "2026-04-17"
    assert rep.step_outputs["strategist"] == {"sentiment_score": 72}
    assert rep.step_outputs["chief_judge"] == {"council_consensus": "agree"}
    assert rep.get_step("missing") == {}


def test_build_replay_is_isolated_from_record_mutation():
    """rep.step_outputs 변경이 record.steps 에 반영되면 안 된다."""
    rec = _record()
    rep = build_replay(rec)
    rep.step_outputs["strategist"]["sentiment_score"] = 0
    assert rec.steps[0].output["sentiment_score"] == 72


# --- async replay wrapper ---


@dataclass
class _FakeResult:
    _rows: list[Any]

    def one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None


@dataclass
class _FakeConn:
    rows: list[Any] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _FakeResult:
        return _FakeResult(self.rows)

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def connect(self) -> _FakeConn:
        return self.conn


@pytest.mark.asyncio
async def test_replay_council_run_returns_none_when_missing():
    engine = _FakeEngine(_FakeConn(rows=[]))
    assert await replay_council_run(engine, "nope") is None


@pytest.mark.asyncio
async def test_replay_council_run_returns_payload_for_existing_run():
    rec = _record()
    row = SimpleNamespace(
        macro_run_id=rec.macro_run_id,
        generated_at=rec.generated_at,
        trigger_reason=rec.trigger_reason,
        gate=rec.gate,
        size_multiplier=rec.size_multiplier,
        reasoning=None,
        top_risks_json=[],
        confidence=None,
        news_digest_ref=None,
        next_review_hint=None,
        prompt_version=None,
        model_used=None,
        cost_usd=None,
        latency_ms=None,
        auto_override=False,
        metadata_json={
            "council_log": {
                "target_date": "2026-04-17",
                "steps": [
                    {"name": "strategist", "output": {"sentiment_score": 72}},
                    {"name": "chief_judge", "output": {"council_consensus": "agree"}},
                ],
                "input": {
                    "briefing_text": "brief",
                    "global_snapshot": {"vix": 15.2},
                    "political_news": ["n1"],
                },
            }
        },
    )
    engine = _FakeEngine(_FakeConn(rows=[row]))
    rep = await replay_council_run(engine, rec.macro_run_id)
    assert rep is not None
    assert rep.record.macro_run_id == rec.macro_run_id
    assert rep.input_context["briefing_text"] == "brief"
    assert rep.step_outputs["chief_judge"]["council_consensus"] == "agree"
