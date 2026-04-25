"""slow_loop/app.py — import/compose smoke."""

from __future__ import annotations

from prime_jennie_runtime.slow_loop import app as slow_app


def test_module_loads():
    assert slow_app.OWNER == "slow_loop"
    assert callable(slow_app.run)


def test_tiered_router_returns_none_without_api_keys(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert slow_app._try_build_tiered_router() is None


def test_tiered_router_none_triggers_none_components(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _FakeRedis:
        pass

    assert slow_app._build_slow_loop_components(_FakeRedis()) is None  # type: ignore[arg-type]
