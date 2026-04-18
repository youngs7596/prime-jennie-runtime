"""persistence._resolve_cost 우선순위 테스트 (Phase 2.13-3).

minyoung-mah 0.1.2 의 metadata["usage"] 가 들어오면 실측 토큰 기반 정확 비용.
없으면 기존 char 기반 추정(±20%).
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.slow_loop.persistence import (
    _estimate_cost,
    _real_cost,
    _resolve_cost,
)


def test_real_cost_uses_exact_tokens():
    # Opus 4.7: $15/$75 per 1M
    cost = _real_cost(
        "claude-opus-4-7",
        {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
    )
    # 1000 * 15/1e6 + 500 * 75/1e6 = 0.015 + 0.0375 = 0.0525
    assert cost == pytest.approx(0.0525)


def test_real_cost_missing_fields_returns_none():
    assert _real_cost("claude-opus-4-7", {}) is None
    assert _real_cost("claude-opus-4-7", {"input_tokens": 100}) is None


def test_real_cost_unknown_model_returns_none():
    assert _real_cost("mystery-model", {"input_tokens": 1, "output_tokens": 1}) is None


def test_resolve_cost_prefers_explicit_cost_usd():
    cost = _resolve_cost(
        {"cost_usd": 0.42},
        "claude-opus-4-7",
        prompt_chars=10000,
        output_chars=5000,
    )
    assert cost == 0.42


def test_resolve_cost_prefers_real_usage_over_chars():
    cost = _resolve_cost(
        {"usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}},
        "claude-opus-4-7",
        prompt_chars=99999,  # char estimate 와 확연히 다른 값
        output_chars=99999,
    )
    assert cost == pytest.approx(0.0525)


def test_resolve_cost_falls_back_to_chars_when_no_usage():
    cost = _resolve_cost(
        {},
        "claude-opus-4-7",
        prompt_chars=2000,
        output_chars=1000,
    )
    # _estimate_tokens: 2000//2=1000, 1000//2=500
    # 1000 * 15/1e6 + 500 * 75/1e6 = 0.0525
    expected = _estimate_cost("claude-opus-4-7", 2000, 1000)
    assert cost == expected


def test_resolve_cost_returns_none_when_nothing_available():
    assert _resolve_cost({}, "claude-opus-4-7", prompt_chars=None, output_chars=0) is None


def test_resolve_cost_deepseek_chat_pricing():
    cost = _resolve_cost(
        {"usage": {"input_tokens": 10000, "output_tokens": 2000, "total_tokens": 12000}},
        "deepseek-chat",
        prompt_chars=None,
        output_chars=0,
    )
    # DeepSeek chat: $0.27 / $1.10 per 1M
    # 10000 * 0.27/1e6 + 2000 * 1.10/1e6 = 0.0027 + 0.0022 = 0.0049
    assert cost == pytest.approx(0.0049)
