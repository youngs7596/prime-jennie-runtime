"""MacroContextBuilder stub feeder 기반 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.slow_loop.macro.closed_conditions import check_closed_conditions
from prime_jennie_runtime.slow_loop.macro.context_builder import MacroContextBuilder
from prime_jennie_runtime.slow_loop.macro.feeders.stub import (
    StubKorMacroNewsFeeder,
    StubMarketSnapshotFeeder,
    StubWsjDigestFeeder,
)


@pytest.mark.asyncio
async def test_builder_assembles_context():
    builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )
    ctx = await builder.build(as_of=datetime(2026, 4, 16, 8, 0, tzinfo=UTC))
    assert ctx.wsj_digest_id == "wsj_stub_0000"
    assert "반도체" not in ctx.wsj_headlines_formatted  # stub 내용
    assert ctx.market_snapshot.kospi.close == 2800.0
    assert ctx.trigger_reason == "scheduled_0800"


@pytest.mark.asyncio
async def test_stub_snapshot_has_no_triggers():
    """Stub 스냅샷은 정상 장 — closed 조건 아무것도 트리거 안 됨."""
    builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )
    ctx = await builder.build(as_of=datetime(2026, 4, 16, 8, 0, tzinfo=UTC))
    triggers = check_closed_conditions(ctx.market_snapshot)
    assert triggers == []
