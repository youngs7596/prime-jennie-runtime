"""ScreeningToolAdapter — subprocess(같은 인터프리터)를 실제로 spawn해서 검증.

backend 은 subprocess 단일 (Phase 2.10 정리 — 과거 docker 분기 제거). 실제
컨테이너 격리는 slow-loop 호스트 측 보안 + executor.py AST 화이트리스트로
확보한다.
"""

from __future__ import annotations

import asyncio

import pytest

from prime_jennie_runtime.screening_executor.adapter import (
    EXECUTOR_MODULE,
    ScreeningToolAdapter,
)
from prime_jennie_runtime.screening_executor.schemas import ScreeningResult

UNIVERSE = ["005930", "000660"]
CTX = {"as_of": "2026-04-17", "universe": UNIVERSE}


HAPPY_CODE = (
    "def screen(market_data, context):\n"
    "    return [{\n"
    "        'ticker': '005930',\n"
    "        'strategy_tag': 'SECTOR_MOMENTUM',\n"
    "        'conviction': 0.7,\n"
    "        'entry_hint': {'trigger': 'limit', 'price_hint': 71000.0},\n"
    "        'factors': {'momentum_5d': 0.034},\n"
    "        'notes': 'reason',\n"
    "    }]\n"
)


# ---------- subprocess (실 spawn) ----------


@pytest.mark.asyncio
async def test_subprocess_happy_path_returns_candidates():
    adapter = ScreeningToolAdapter(timeout_s=30.0)
    candidates = await adapter.invoke(HAPPY_CODE, CTX)
    assert len(candidates) == 1
    assert candidates[0].ticker == "005930"


@pytest.mark.asyncio
async def test_subprocess_run_returns_result_with_timing():
    adapter = ScreeningToolAdapter(timeout_s=30.0)
    result: ScreeningResult = await adapter.run(HAPPY_CODE, CTX)
    assert result.ok
    assert result.exec_time_ms >= 0


@pytest.mark.asyncio
async def test_subprocess_import_violation_returns_empty():
    code = "import os\ndef screen(md, ctx):\n    return []\n"
    adapter = ScreeningToolAdapter(timeout_s=30.0)
    candidates = await adapter.invoke(code, CTX)
    assert candidates == []


@pytest.mark.asyncio
async def test_subprocess_runtime_error_returns_empty():
    code = "def screen(md, ctx):\n    raise ValueError('boom')\n"
    adapter = ScreeningToolAdapter(timeout_s=30.0)
    result = await adapter.run(code, CTX)
    assert not result.ok
    assert result.error == "runtime_error"


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_child():
    """무한 루프 — adapter가 timeout으로 child kill하고 ScreeningResult(timeout) 반환."""
    code = "def screen(md, ctx):\n    while True:\n        pass\n"
    adapter = ScreeningToolAdapter(timeout_s=1.0)
    result = await adapter.run(code, CTX)
    assert not result.ok
    assert result.error == "timeout"


# ---------- argv 구성 ----------


def test_subprocess_argv_uses_python_module():
    adapter = ScreeningToolAdapter()
    argv = adapter._build_argv()
    assert argv[1:3] == ["-m", EXECUTOR_MODULE]


# ---------- stdout 파싱 ----------


def test_parse_stdout_handles_empty():
    adapter = ScreeningToolAdapter()
    r = adapter._parse_stdout(b"")
    assert not r.ok
    assert r.error == "subprocess_error"
    assert r.details == "empty stdout"


def test_parse_stdout_handles_garbage():
    adapter = ScreeningToolAdapter()
    r = adapter._parse_stdout(b"not json at all\n")
    assert not r.ok
    assert r.error == "subprocess_error"
    assert r.details and r.details.startswith("stdout parse failed")


def test_parse_stdout_takes_last_line():
    """executor가 print 등으로 앞 줄을 더럽혀도 마지막 줄에서 결과를 찾는다."""
    valid = ScreeningResult(ok=True).model_dump_json().encode()
    adapter = ScreeningToolAdapter()
    r = adapter._parse_stdout(b"warning: blah\n" + valid + b"\n")
    assert r.ok


# ---------- subprocess binary 부재 ----------


@pytest.mark.asyncio
async def test_executor_binary_not_found():
    """asyncio.create_subprocess_exec가 FileNotFoundError를 일으키면 subprocess_error로 감싼다."""
    adapter = ScreeningToolAdapter()
    # _build_argv 를 mock — PATH 미존재 바이너리 명시
    adapter._build_argv = lambda: ["/nonexistent/__no_such_executor__"]
    result = await adapter.run("def screen(m,c): return []", CTX)
    assert not result.ok
    assert result.error == "subprocess_error"


# ---------- invoke 시그니처: context None 기본값 ----------


@pytest.mark.asyncio
async def test_invoke_accepts_none_context():
    adapter = ScreeningToolAdapter(timeout_s=30.0)
    candidates = await adapter.invoke(HAPPY_CODE, None)
    assert len(candidates) == 1


# ---------- 빈 stdout (subprocess가 아무것도 출력 안 함) ----------


@pytest.mark.asyncio
async def test_subprocess_silent_child(monkeypatch):
    """child가 정상 종료했지만 stdout이 비어있으면 subprocess_error."""
    adapter = ScreeningToolAdapter(timeout_s=5.0)
    adapter._build_argv = lambda: ["/bin/true"]
    result = await adapter.run("noop", CTX)
    assert not result.ok
    assert result.error == "subprocess_error"


# ---------- exit non-zero ----------


@pytest.mark.asyncio
async def test_subprocess_nonzero_exit():
    adapter = ScreeningToolAdapter(timeout_s=5.0)
    adapter._build_argv = lambda: ["/bin/sh", "-c", "echo oops 1>&2; exit 2"]
    result = await adapter.run("noop", CTX)
    assert not result.ok
    assert result.error == "subprocess_error"
    assert result.details and "oops" in result.details


def test_async_invoke_runnable():
    """잘못된 syntax-level 검사: invoke가 coroutine 반환이고 await 없이는 동작 안 함."""
    adapter = ScreeningToolAdapter(timeout_s=5.0)
    coro = adapter.invoke("def screen(m,c): return []", CTX)
    # 명시적으로 close → 경고 없이 정리
    coro.close()
    # 다시 별도 호출은 asyncio.run으로
    candidates = asyncio.run(adapter.invoke("def screen(m,c): return []", CTX))
    assert candidates == []
