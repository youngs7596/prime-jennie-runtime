"""Council 런 리플레이 지원.

저장된 `CouncilRunRecord` 를 원본 입력 + 각 step 출력 단위로 꺼내 쓰기 쉽게 가공.
실제 LLM 재호출은 이 모듈이 아닌 slow_loop 측 책임 — 여기는 "재생 가능한 형태로
복원"만 담당.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .persistence import fetch_council_run
from .records import CouncilRunRecord


@dataclass
class CouncilReplay:
    """Replay payload."""

    record: CouncilRunRecord
    input_context: dict[str, Any]
    step_outputs: dict[str, dict[str, Any]]

    def get_step(self, name: str) -> dict[str, Any]:
        return self.step_outputs.get(name, {})


def build_replay(record: CouncilRunRecord) -> CouncilReplay:
    """레코드 → 리플레이 페이로드."""
    input_context = {
        "briefing_text": record.input_briefing_text,
        "global_snapshot": record.input_global_snapshot,
        "political_news": list(record.input_political_news),
        "sector_momentum_text": record.input_sector_momentum_text,
        "index_technical_text": record.input_index_technical_text,
        "target_date": record.target_date.isoformat() if record.target_date else None,
    }
    step_outputs = {s.name: dict(s.output) for s in record.steps}
    return CouncilReplay(
        record=record,
        input_context=input_context,
        step_outputs=step_outputs,
    )


async def replay_council_run(engine: AsyncEngine, macro_run_id: str) -> CouncilReplay | None:
    """macro_run_id 로 리플레이 페이로드 구성."""
    record = await fetch_council_run(engine, macro_run_id)
    if record is None:
        return None
    return build_replay(record)
