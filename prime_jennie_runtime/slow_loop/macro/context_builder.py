"""MacroContext 조립 유틸."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .feeders.base import KorMacroNewsFeeder, MarketSnapshotFeeder, WsjDigestFeeder
from .schemas import MacroContext, RecentMacroRun


@dataclass
class MacroContextBuilder:
    """Macro Gate 프롬프트 입력 조립."""

    wsj: WsjDigestFeeder
    market: MarketSnapshotFeeder
    kor: KorMacroNewsFeeder

    async def build(
        self,
        as_of: datetime,
        recent_runs: list[RecentMacroRun] | None = None,
        trigger_reason: str = "scheduled_0800",
    ) -> MacroContext:
        digest_id, summary, headlines = await self.wsj.fetch(as_of)
        snapshot = await self.market.fetch(as_of)
        kor = await self.kor.fetch(as_of)
        return MacroContext(
            as_of=as_of,
            trigger_reason=trigger_reason,
            market_snapshot=snapshot,
            wsj_digest_id=digest_id,
            wsj_macro_summary=summary,
            wsj_headlines_formatted=headlines,
            kor_macro_news_formatted=kor,
            recent_runs=list(recent_runs or []),
        )
