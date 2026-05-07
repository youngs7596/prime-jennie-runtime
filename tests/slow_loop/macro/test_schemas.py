"""MacroGateOutput schema 회귀 테스트."""

from __future__ import annotations

from prime_jennie_runtime.slow_loop.macro.schemas import MacroGateOutput, RiskItem


def _base_kwargs(reasoning: str) -> dict:
    return {
        "gate": "open",
        "size_multiplier": 0.75,
        "reasoning": reasoning,
        "top_risks": [
            RiskItem(category="other", severity="low", description="테스트용 더미 위험"),
        ],
        "confidence": "medium",
        "news_digest_ref": "nd_test",
    }


def test_reasoning_long_string_does_not_fail_validation():
    long = "결론: open. " + ("매우 길고 자세한 reasoning 텍스트 " * 200)
    assert len(long) > 2000
    out = MacroGateOutput(**_base_kwargs(reasoning=long))
    assert out.gate == "open"
    assert out.size_multiplier == 0.75
    assert len(out.reasoning) == 2000


def test_reasoning_short_string_passes_through():
    out = MacroGateOutput(**_base_kwargs(reasoning="간단한 reasoning"))
    assert out.reasoning == "간단한 reasoning"
