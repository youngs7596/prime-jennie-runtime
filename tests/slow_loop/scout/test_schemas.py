"""Scout Pydantic 스키마 검증 테스트."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prime_jennie_runtime.slow_loop.scout.schemas import (
    EntryHint,
    ScoutOutput,
    ScreeningCandidate,
)


def test_entry_hint_limit():
    hint = EntryHint(trigger="limit", price_hint=71200)
    assert hint.price_hint == 71200


def test_entry_hint_market_no_price():
    hint = EntryHint(trigger="market")
    assert hint.price_hint is None


def test_screening_candidate_basic():
    cand = ScreeningCandidate(
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        conviction=0.72,
        entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
        factors={"momentum_5d": 0.034},
        notes="반도체 모멘텀",
    )
    assert cand.conviction == 0.72
    assert cand.exit_hint is None


def test_screening_candidate_conviction_out_of_range():
    with pytest.raises(ValidationError):
        ScreeningCandidate(
            ticker="005930",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=1.5,
            entry_hint=EntryHint(trigger="market"),
        )


def test_scout_output_basic():
    out = ScoutOutput(
        screening_code="def screen(md, ctx): return []",
        hypothesis="반도체 모멘텀 가설",
        expected_candidates=5,
        factor_weights={"momentum": 0.4, "news": 0.3, "eps_rev": 0.3},
        strategy_tags_used=["SECTOR_MOMENTUM"],
    )
    assert out.fallback_strategy == "skip_today"
    assert out.estimated_runtime_seconds == 10.0


def test_scout_output_hypothesis_too_long():
    with pytest.raises(ValidationError):
        ScoutOutput(
            screening_code="def screen(md, ctx): return []",
            hypothesis="가" * 201,
            expected_candidates=5,
            factor_weights={},
            strategy_tags_used=["SECTOR_MOMENTUM"],
        )


def test_scout_output_expected_candidates_cap():
    with pytest.raises(ValidationError):
        ScoutOutput(
            screening_code="def screen(md, ctx): return []",
            hypothesis="h",
            expected_candidates=21,
            factor_weights={},
            strategy_tags_used=["SECTOR_MOMENTUM"],
        )
