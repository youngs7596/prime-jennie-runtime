"""IntentRouter — DeepSeek 분류 + 안전 가드."""

from __future__ import annotations

import json

import httpx
import pytest

from prime_jennie_runtime.telegram_bot.llm_intent import IntentRouter


def _mock_transport(response_json: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_json)

    return httpx.MockTransport(handler)


def _ds_payload(content: dict | str) -> dict:
    txt = content if isinstance(content, str) else json.dumps(content)
    return {"choices": [{"message": {"content": txt}}]}


@pytest.mark.asyncio
async def test_disabled_when_no_api_key():
    router = IntentRouter(api_key="")
    assert router.enabled is False
    assert await router.classify("오늘 손익") is None
    await router.close()


@pytest.mark.asyncio
async def test_safe_command_routed():
    transport = _mock_transport(_ds_payload({"command": "/balance", "args": ""}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("현금 얼마 있어?")
    assert result == ("/balance", "")


@pytest.mark.asyncio
async def test_args_passed_through():
    transport = _mock_transport(_ds_payload({"command": "/price", "args": "삼성전자"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("삼성전자 가격")
    assert result == ("/price", "삼성전자")


@pytest.mark.asyncio
async def test_dangerous_command_blocked():
    """LLM 이 /buy 추출해도 안전 가드로 None."""
    transport = _mock_transport(_ds_payload({"command": "/buy", "args": "005930 10"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("삼성전자 10주 사")
    assert result is None


@pytest.mark.asyncio
async def test_unknown_command_returns_none():
    transport = _mock_transport(_ds_payload({"command": "/unknown_cmd", "args": ""}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("뭔가")
    assert result is None


@pytest.mark.asyncio
async def test_null_command_returns_none():
    transport = _mock_transport(_ds_payload({"command": None, "reason": "분류 불가"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("랜덤 잡담")
    assert result is None


@pytest.mark.asyncio
async def test_api_error_returns_none():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("뭐 해줘")
    assert result is None


@pytest.mark.asyncio
async def test_invalid_json_returns_none():
    transport = _mock_transport(_ds_payload("not json"))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("이상한 메시지")
    assert result is None
