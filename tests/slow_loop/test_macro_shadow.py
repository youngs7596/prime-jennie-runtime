"""매크로 게이트 그림자(Claude) 안전장치 테스트.

설계: .ai/designs/2026-11-02-macro-gate-claude-shadow.md
- 하루 호출 상한 (Redis 카운터, KST 날짜 기준)
- 시간 상한 (늦은 그림자가 주 판단을 붙잡지 않게)
- 실패는 예외가 아니라 {"error": ...} 로 돌아온다
- Claude 모델은 구조화 출력을 JSON 스키마 방식으로 부른다
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.slow_loop import app as slow_app
from prime_jennie_runtime.slow_loop import pipeline as pl


class _FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


class _BrokenRedis:
    async def incr(self, key: str) -> int:
        raise ConnectionError("redis down")


# ---------------------------------------------------------------------------
# 하루 호출 상한
# ---------------------------------------------------------------------------


async def test_budget_allows_up_to_cap_then_blocks(monkeypatch):
    monkeypatch.setattr(pl, "SHADOW_DAILY_CAP", 3)
    r = _FakeRedis()
    now = datetime(2026, 11, 3, 8, 30, tzinfo=KST)
    results = [await pl._shadow_budget_ok(r, now) for _ in range(4)]
    assert results == [True, True, True, False]
    # 첫 호출에만 만료를 건다 (이틀)
    assert r.expires == {"macro:shadow:calls:2026-11-03": 2 * 24 * 3600}


async def test_budget_counter_uses_kst_date():
    r = _FakeRedis()
    # UTC 11-02 23:30 = KST 11-03 08:30 — 키는 KST 날짜여야 한다
    from datetime import UTC

    await pl._shadow_budget_ok(r, datetime(2026, 11, 2, 23, 30, tzinfo=UTC))
    assert list(r.counts) == ["macro:shadow:calls:2026-11-03"]


async def test_budget_without_redis_or_on_error_does_not_block():
    assert await pl._shadow_budget_ok(None) is True
    assert await pl._shadow_budget_ok(_BrokenRedis()) is True


# ---------------------------------------------------------------------------
# 그림자 실행 — 시간 상한·실패 격리
# ---------------------------------------------------------------------------


class _SlowOrchestrator:
    async def run_pipeline(self, pipeline: Any, user_request: str = "") -> Any:
        await asyncio.sleep(5)
        raise AssertionError("시간 상한에 걸려 여기까지 오면 안 된다")


class _OkOrchestrator:
    def __init__(self) -> None:
        self.calls = 0

    async def run_pipeline(self, pipeline: Any, user_request: str = "") -> Any:
        self.calls += 1
        step = SimpleNamespace(outputs=[SimpleNamespace(metadata={"usage": {"input_tokens": 1}})])
        return SimpleNamespace(completed=True, state={"macro_gate": step})


class _Ctx:
    def model_copy(self, update: dict[str, Any]) -> _Ctx:
        return self


def _comp(orch: Any, redis: Any = None) -> Any:
    return SimpleNamespace(shadow_orchestrator=orch, redis_client=redis)


async def test_shadow_off_returns_none():
    assert await pl._run_shadow_macro(_comp(None), _Ctx(), observer=None) is None


async def test_shadow_timeout_returns_error_not_raise(monkeypatch):
    monkeypatch.setattr(pl, "SHADOW_TIMEOUT_S", 0.05)
    out = await pl._run_shadow_macro(_comp(_SlowOrchestrator()), _Ctx(), observer=None)
    assert out == {"error": "timeout_0s"}


async def test_shadow_success_carries_usage_metadata():
    orch = _OkOrchestrator()
    out = await pl._run_shadow_macro(_comp(orch, _FakeRedis()), _Ctx(), observer=None)
    assert out is not None and "error" not in out
    assert out["metadata"] == {"usage": {"input_tokens": 1}}
    assert orch.calls == 1


async def test_shadow_over_cap_skips_without_calling_model(monkeypatch):
    monkeypatch.setattr(pl, "SHADOW_DAILY_CAP", 1)
    orch = _OkOrchestrator()
    r = _FakeRedis()
    first = await pl._run_shadow_macro(_comp(orch, r), _Ctx(), observer=None)
    second = await pl._run_shadow_macro(_comp(orch, r), _Ctx(), observer=None)
    assert "error" not in first
    assert second == {"error": "daily_cap_exceeded"}
    assert orch.calls == 1


# ---------------------------------------------------------------------------
# Claude 모델 래퍼 — JSON 스키마 방식 고정
# ---------------------------------------------------------------------------


def test_claude_shadow_defaults_structured_output_to_json_schema():
    captured: dict[str, Any] = {}

    class _FakeChatAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs

        def with_structured_output(self, schema: Any, *, method: Any = None, **kwargs: Any):
            captured["method"] = method
            captured["kwargs"] = kwargs
            return "handle"

    model = slow_app._make_claude_shadow(_FakeChatAnthropic, "claude-opus-5-5", "k")
    # 하네스는 method 없이 include_raw 만 넘긴다
    assert model.with_structured_output(dict, include_raw=True) == "handle"
    assert captured["method"] == "json_schema"
    assert captured["kwargs"] == {"include_raw": True}
    # 명시하면 그대로 존중
    model.with_structured_output(dict, method="function_calling")
    assert captured["method"] == "function_calling"
    # sampling 파라미터는 넘기지 않는다 (최신 모델 400)
    assert captured["init"] == {"model": "claude-opus-5-5", "api_key": "k", "max_tokens": 16000}


def test_default_anthropic_model_is_opus_5_5(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    from prime_jennie_runtime.slow_loop.persistence import _tier_model

    assert slow_app.DEFAULT_ANTHROPIC_MODEL == "claude-opus-5-5"
    assert _tier_model("shadow_reasoning") == "claude-opus-5-5"


@pytest.mark.parametrize("model", ["claude-opus-5-5"])
def test_real_langchain_wrapper_builds_without_network(model):
    langchain_anthropic = pytest.importorskip("langchain_anthropic")
    m = slow_app._make_claude_shadow(langchain_anthropic.ChatAnthropic, model, "sk-test")
    assert m.model == model
    assert m.max_tokens == 16000
