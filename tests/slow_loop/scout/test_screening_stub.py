"""ScreeningToolAdapterStub 테스트."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.slow_loop.scout.screening_stub import ScreeningToolAdapterStub


@pytest.mark.asyncio
async def test_default_stub_returns_three_candidates():
    stub = ScreeningToolAdapterStub()
    cands = await stub.invoke("any code", {})
    assert len(cands) == 3
    assert cands[0].ticker == "005930"
    # conviction 내림차순 아님은 OK — engine이 정렬 불요, 구조만 맞으면 됨
    assert all(0.0 <= c.conviction <= 1.0 for c in cands)


@pytest.mark.asyncio
async def test_custom_candidates_returned():
    from prime_jennie_runtime.slow_loop.scout.schemas import EntryHint, ScreeningCandidate

    custom = [
        ScreeningCandidate(
            ticker="035420",
            strategy_tag="GAP_UP_REBOUND",
            conviction=0.9,
            entry_hint=EntryHint(trigger="limit", price_hint=200000.0),
        )
    ]
    stub = ScreeningToolAdapterStub(candidates=custom)
    cands = await stub.invoke("code", {})
    assert len(cands) == 1
    assert cands[0].ticker == "035420"


@pytest.mark.asyncio
async def test_code_and_context_are_ignored():
    """Stub은 어떤 코드/컨텍스트도 받아도 동일 결과."""
    stub = ScreeningToolAdapterStub()
    a = await stub.invoke("def screen(md,ctx): return []", {"as_of": "2026-04-16"})
    b = await stub.invoke("completely different code", {})
    assert [c.ticker for c in a] == [c.ticker for c in b]
