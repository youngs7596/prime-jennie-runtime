"""sheet_id 생성 테스트."""

from __future__ import annotations

from datetime import datetime

import pytest

from prime_jennie_runtime.position_sheet.schema import KST, SHEET_ID_RE
from prime_jennie_runtime.slow_loop.strategy.sheet_id import generate_sheet_id


def test_format_matches_regex():
    sid = generate_sheet_id("005930", datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST))
    assert SHEET_ID_RE.match(sid), f"sheet_id {sid} does not match schema regex"


def test_same_inputs_same_id():
    """멱등성: 같은 입력이면 같은 sheet_id."""
    ts = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    assert generate_sheet_id("005930", ts) == generate_sheet_id("005930", ts)


def test_different_ticker_different_id():
    ts = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    assert generate_sheet_id("005930", ts) != generate_sheet_id("000660", ts)


def test_different_time_different_id():
    ts1 = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    ts2 = datetime(2026, 4, 16, 9, 15, 1, tzinfo=KST)
    assert generate_sheet_id("005930", ts1) != generate_sheet_id("005930", ts2)


def test_invalid_ticker_rejected():
    ts = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    with pytest.raises(ValueError, match="6 digits"):
        generate_sheet_id("5930", ts)
    with pytest.raises(ValueError, match="6 digits"):
        generate_sheet_id("A05930", ts)


def test_salt_changes_id():
    """같은 ticker/ts인데 salt 다르면 다른 id (수동 재시도 용)."""
    ts = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    assert generate_sheet_id("005930", ts) != generate_sheet_id("005930", ts, salt="retry1")
