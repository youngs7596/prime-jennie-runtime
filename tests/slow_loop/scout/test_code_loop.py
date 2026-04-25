"""Scout 코드 검증 닫힌 루프 단위 테스트.

run_scout_once 와 ScreeningToolAdapter.run 을 fake 로 대체해서 다음 시나리오 검증:

1. 1회 성공 — 첫 시도에서 ok=True + candidates 있음 → accepted=True
2. 1차 실패 → 2차 성공 — 첫 시도 sandbox 에러, 두번째 시도 성공
3. 3회 모두 실패 — escalate, accepted=False, attempts 3개
4. 같은 코드 반복 → 즉시 break — code_repeated 사유로 종료
5. 첫 시도부터 빈 candidates (ok=True 인데 후보 0) → 다음 attempt 로 넘어감
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from minyoung_mah import NullObserver

from prime_jennie_runtime.screening_executor.schemas import ScreeningResult
from prime_jennie_runtime.slow_loop.scout import code_loop
from prime_jennie_runtime.slow_loop.scout.schemas import (
    EntryHint,
    MacroStateForScout,
    MarketSummary,
    ScoutContext,
    ScoutOutput,
    ScreeningCandidate,
)


def _ctx() -> ScoutContext:
    return ScoutContext(
        as_of=date(2026, 4, 24),
        universe=["005930", "000660"],
        market_summary=MarketSummary(
            kospi_close=2800.0,
            kospi_change_pct=0.0,
            kosdaq_close=880.0,
            kosdaq_change_pct=0.0,
            up_count=500,
            down_count=300,
        ),
        macro_state=MacroStateForScout(gate="open", size_multiplier=0.75, gate_run_id="m1"),
        news_events={},
        sector_momentum={"반도체": 0.04},
        strategy_tags_available=["SECTOR_MOMENTUM"],
    )


def _scout_out(code: str, hyp: str = "test") -> ScoutOutput:
    return ScoutOutput(
        screening_code=code,
        hypothesis=hyp,
        expected_candidates=2,
        factor_weights={"momentum": 1.0},
        strategy_tags_used=["SECTOR_MOMENTUM"],
    )


def _candidate(ticker: str = "005930") -> ScreeningCandidate:
    return ScreeningCandidate(
        ticker=ticker,
        strategy_tag="SECTOR_MOMENTUM",
        conviction=0.7,
        entry_hint=EntryHint(trigger="market"),
        factors={"momentum_20d": 0.05},
    )


class _FakeStep:
    """run_scout_once 가 반환하는 PipelineStepResult mock."""

    def __init__(self, scout_out: ScoutOutput | None) -> None:
        self._out = scout_out

    def payload_as(self, schema: type) -> Any:  # noqa: ANN401
        return self._out


class _FakeScreening:
    """ScreeningToolAdapter mock — sequenced ScreeningResult 반환."""

    def __init__(self, results: list[ScreeningResult]) -> None:
        self._results = results
        self.calls = 0

    async def run(self, code: str, context: dict) -> ScreeningResult:
        r = self._results[self.calls]
        self.calls += 1
        return r

    async def invoke(self, code: str, context: dict):
        r = await self.run(code, context)
        return list(r.candidates)


def _patch_scout(monkeypatch, scout_outs: list[ScoutOutput | None]) -> list[int]:
    """run_scout_once 를 monkeypatch — sequenced ScoutOutput 반환. call count 리스트도 반환."""
    counter = [0]

    async def fake_run_scout_once(orch, ctx, observer):  # noqa: ARG001
        out = scout_outs[counter[0]]
        counter[0] += 1
        return _FakeStep(out)

    monkeypatch.setattr(code_loop, "run_scout_once", fake_run_scout_once)
    return counter


# =====================================================================
# 시나리오
# =====================================================================


@pytest.mark.asyncio
async def test_first_attempt_success(monkeypatch):
    """첫 시도에서 ok=True + candidates 있음 → accepted=True, attempts=1."""
    counter = _patch_scout(monkeypatch, [_scout_out("def screen(m,c): return [{}]")])
    screening = _FakeScreening(
        [ScreeningResult(ok=True, candidates=[_candidate("005930"), _candidate("000660")])]
    )

    result = await code_loop.generate_validated_code(
        orch=None,  # fake 가 안 씀
        scout_ctx=_ctx(),
        screening=screening,
        screening_context={},
        observer=NullObserver(),
    )

    assert result.accepted is True
    assert len(result.attempts) == 1
    assert len(result.result.candidates) == 2
    assert result.early_break_reason is None
    assert counter[0] == 1
    assert screening.calls == 1


@pytest.mark.asyncio
async def test_first_fail_second_success(monkeypatch):
    """1차 sandbox 에러 → 2차 성공. attempts=2, accepted=True."""
    counter = _patch_scout(
        monkeypatch,
        [
            _scout_out("def screen(m,c): import np\n    return []"),  # 잘못된 코드
            _scout_out("def screen(m,c): return [{}]"),  # 수정된 코드
        ],
    )
    screening = _FakeScreening(
        [
            ScreeningResult(ok=False, error="runtime_error", details="NameError: np"),
            ScreeningResult(ok=True, candidates=[_candidate("005930")]),
        ]
    )

    result = await code_loop.generate_validated_code(
        orch=None,
        scout_ctx=_ctx(),
        screening=screening,
        screening_context={},
        observer=NullObserver(),
    )

    assert result.accepted is True
    assert len(result.attempts) == 2
    assert result.attempts[0].result.ok is False
    assert result.attempts[0].result.error == "runtime_error"
    assert result.attempts[1].result.ok is True
    assert counter[0] == 2


@pytest.mark.asyncio
async def test_three_failures_escalate(monkeypatch):
    """3회 모두 실패 → accepted=False, attempts=3, last_error 노출."""
    counter = _patch_scout(
        monkeypatch,
        [_scout_out(f"def screen(m,c): pass # v{i}") for i in range(3)],
    )
    screening = _FakeScreening(
        [
            ScreeningResult(ok=False, error="invalid_return_type", details="not a list"),
            ScreeningResult(ok=False, error="runtime_error", details="KeyError"),
            ScreeningResult(ok=False, error="runtime_error", details="TypeError"),
        ]
    )

    result = await code_loop.generate_validated_code(
        orch=None,
        scout_ctx=_ctx(),
        screening=screening,
        screening_context={},
        observer=NullObserver(),
    )

    assert result.accepted is False
    assert len(result.attempts) == 3
    assert result.result.error == "runtime_error"
    assert counter[0] == 3
    assert screening.calls == 3


@pytest.mark.asyncio
async def test_repeated_code_breaks_early(monkeypatch):
    """LLM 이 같은 코드를 두 번 생성하면 즉시 break (재실행 무의미)."""
    same_code = "def screen(m,c): import np\n    return []"
    counter = _patch_scout(
        monkeypatch,
        [
            _scout_out(same_code),  # 1차
            _scout_out(same_code),  # 2차 — 같은 코드 → break
            _scout_out(same_code),  # 호출되지 않아야
        ],
    )
    screening = _FakeScreening(
        [ScreeningResult(ok=False, error="runtime_error", details="NameError: np")]
    )

    result = await code_loop.generate_validated_code(
        orch=None,
        scout_ctx=_ctx(),
        screening=screening,
        screening_context={},
        observer=NullObserver(),
    )

    assert result.accepted is False
    assert result.early_break_reason == "code_repeated"
    assert len(result.attempts) == 1  # 1차만 누적, 2차는 hash 매칭으로 break
    assert counter[0] == 2  # scout 는 2번 호출됨
    assert screening.calls == 1  # sandbox 는 1번만


@pytest.mark.asyncio
async def test_ok_but_empty_candidates_continues(monkeypatch):
    """ok=True 인데 candidates 0개도 실패 취급 → 다음 attempt."""
    counter = _patch_scout(
        monkeypatch,
        [
            _scout_out("def screen(m,c): return []"),
            _scout_out("def screen(m,c): return [{}]"),
        ],
    )
    screening = _FakeScreening(
        [
            ScreeningResult(ok=True, candidates=[]),  # 빈 결과 → 다음
            ScreeningResult(ok=True, candidates=[_candidate("005930")]),
        ]
    )

    result = await code_loop.generate_validated_code(
        orch=None,
        scout_ctx=_ctx(),
        screening=screening,
        screening_context={},
        observer=NullObserver(),
    )

    assert result.accepted is True
    assert len(result.attempts) == 2
    assert counter[0] == 2


@pytest.mark.asyncio
async def test_previous_attempts_injected_into_ctx(monkeypatch):
    """재시도 시 ScoutContext.previous_attempts 에 직전 시도 정보가 채워진다."""
    captured_ctxs: list[ScoutContext] = []

    async def fake_run_scout_once(orch, ctx, observer):  # noqa: ARG001
        captured_ctxs.append(ctx)
        out_idx = len(captured_ctxs) - 1
        codes = ["def screen(m,c): bad", "def screen(m,c): return [{}]"]
        return _FakeStep(_scout_out(codes[out_idx], hyp=f"v{out_idx}"))

    monkeypatch.setattr(code_loop, "run_scout_once", fake_run_scout_once)
    screening = _FakeScreening(
        [
            ScreeningResult(ok=False, error="compile_error", details="SyntaxError"),
            ScreeningResult(ok=True, candidates=[_candidate("005930")]),
        ]
    )

    result = await code_loop.generate_validated_code(
        orch=None,
        scout_ctx=_ctx(),
        screening=screening,
        screening_context={},
        observer=NullObserver(),
    )

    assert result.accepted is True
    # 1차 호출엔 previous_attempts 비어있음, 2차엔 1개 누적
    assert len(captured_ctxs) == 2
    assert captured_ctxs[0].previous_attempts == []
    assert len(captured_ctxs[1].previous_attempts) == 1
    hint = captured_ctxs[1].previous_attempts[0]
    assert hint.attempt_no == 1
    assert hint.error == "compile_error"
    assert "SyntaxError" in hint.details
