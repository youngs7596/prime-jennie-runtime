"""slow_loop/app.py — import/compose smoke."""

from __future__ import annotations

import os

from prime_jennie_runtime.slow_loop import app as slow_app


def test_module_loads():
    assert slow_app.OWNER == "slow_loop"
    assert callable(slow_app.run)


def test_components_returns_none_without_vllm_env(monkeypatch):
    monkeypatch.delenv("VLLM_LLM_URL", raising=False)
    monkeypatch.delenv("VLLM_LLM_MODEL", raising=False)
    assert slow_app._try_build_chat_model() is None


def test_chat_model_none_triggers_none_components(monkeypatch):
    monkeypatch.delenv("VLLM_LLM_URL", raising=False)
    monkeypatch.delenv("VLLM_LLM_MODEL", raising=False)

    class _FakeRedis:
        pass

    assert slow_app._build_slow_loop_components(_FakeRedis()) is None  # type: ignore[arg-type]
