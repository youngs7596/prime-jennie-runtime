"""ScreeningToolAdapterStub — Track D 스크리닝 실행 컨테이너의 자리 표시자.

Scout가 생성한 Python 코드를 실제로 실행하는 건 Track D(`screening_executor/`)의
Docker 격리 컨테이너 책임. Track B 범위에서는 그 자리에 고정 candidates를 반환하는
stub을 둬서 파이프라인 E2E가 가능하게 한다.

Track D가 완성되면 이 모듈을 `ScreeningToolAdapter`(실 구현)로 교체. 시그니처는 고정.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from prime_jennie_runtime.screening_executor.schemas import ScreeningResult

from .schemas import EntryHint, ScreeningCandidate


@runtime_checkable
class ScreeningInvoker(Protocol):
    """Scout 생성 코드를 실행해 후보 리스트를 돌려주는 인터페이스.

    Stub/Real(Track D) 둘 다 이 시그니처를 지키면 slow_loop.pipeline 이 swap 가능.

    - ``invoke``: 빈 리스트 fallback (운영 호환)
    - ``run``: ScreeningResult 풍부한 형태 (검증 루프용 — error/details 노출)
    """

    async def invoke(self, code: str, context: dict) -> list[ScreeningCandidate]: ...
    async def run(self, code: str, context: dict) -> ScreeningResult: ...


@dataclass
class ScreeningToolAdapterStub:
    """고정 candidates를 반환하는 stub. 생성 인자로 원하는 후보를 주입 가능."""

    candidates: list[ScreeningCandidate] = field(
        default_factory=lambda: [
            ScreeningCandidate(
                ticker="005930",
                strategy_tag="SECTOR_MOMENTUM",
                conviction=0.72,
                entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
                factors={"momentum_5d": 0.034, "news_score": 0.3},
                notes="반도체 모멘텀 (stub)",
            ),
            ScreeningCandidate(
                ticker="000660",
                strategy_tag="SECTOR_MOMENTUM",
                conviction=0.65,
                entry_hint=EntryHint(trigger="limit", price_hint=135000.0),
                factors={"momentum_5d": 0.028, "news_score": 0.2},
                notes="하이닉스 모멘텀 (stub)",
            ),
            ScreeningCandidate(
                ticker="207940",
                strategy_tag="EARNINGS_DRIFT",
                conviction=0.58,
                entry_hint=EntryHint(trigger="market"),
                factors={"eps_revision_1m": 0.08, "news_score": 0.4},
                notes="삼성바이오 실적 드리프트 (stub)",
            ),
        ]
    )

    async def invoke(self, code: str, context: dict) -> list[ScreeningCandidate]:
        """실제 실행 대신 고정 candidates 반환. code/context는 무시."""
        return list(self.candidates)

    async def run(self, code: str, context: dict) -> ScreeningResult:
        """검증 루프용 — invoke 결과를 ok=True 로 wrap."""
        return ScreeningResult(ok=True, candidates=list(self.candidates))
