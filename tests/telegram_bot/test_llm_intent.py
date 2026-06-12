"""IntentRouter v2 — DeepSeek 분류 + 안전 가드 + 무응답 제거 (사람-승인 매매 §3-3)."""

from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest

from prime_jennie_runtime.telegram_bot.llm_intent import (
    RESPONSE_NL_ERROR,
    RESPONSE_NL_NOT_UNDERSTOOD,
    IntentRouter,
)


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
    assert result is not None
    assert (result.command, result.args) == ("/balance", "")
    assert result.reply is None


@pytest.mark.asyncio
async def test_args_passed_through():
    transport = _mock_transport(_ds_payload({"command": "/price", "args": "삼성전자"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("삼성전자 가격")
    assert result is not None
    assert (result.command, result.args) == ("/price", "삼성전자")


@pytest.mark.asyncio
async def test_adopt_routed_as_safe():
    """조건부 매도 의도 → /adopt 라우팅 (자체 확인 단계가 있어 echo 까지만 감)."""
    transport = _mock_transport(_ds_payload({"command": "/adopt", "args": "서진시스템 -1"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("서진시스템이 양전하거나 -1% 위로 회복하면 매도해줘")
    assert result is not None
    assert (result.command, result.args) == ("/adopt", "서진시스템 -1")


@pytest.mark.asyncio
async def test_confirm_token_stripped_from_nl_args():
    """LLM 출력에 "확인" 이 섞여도 NL 경로로는 확인 단계를 통과할 수 없다."""
    transport = _mock_transport(_ds_payload({"command": "/adopt", "args": "서진시스템 -1 확인"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("서진시스템 -1 등록 확인해줘")
    assert result is not None
    assert result.args == "서진시스템 -1"

    transport = _mock_transport(_ds_payload({"command": "/resume", "args": "확인"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("재개 확인")
    assert result is not None
    assert result.command == "/resume"
    assert result.args == ""  # bare /resume → 확인 안내만 받게 됨


@pytest.mark.asyncio
async def test_dangerous_command_replies_guide():
    """LLM 이 /buy 추출해도 실행 대신 직접 입력 안내 회신 (무응답 아님)."""
    transport = _mock_transport(_ds_payload({"command": "/buy", "args": "005930 10"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("삼성전자 10주 사")
    assert result is not None
    assert result.command is None
    assert result.reply is not None
    assert "/buy" in result.reply


@pytest.mark.asyncio
async def test_unknown_command_replies_not_understood():
    transport = _mock_transport(_ds_payload({"command": "/unknown_cmd", "args": ""}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("뭔가")
    assert result is not None
    assert result.command is None
    assert result.reply == RESPONSE_NL_NOT_UNDERSTOOD


@pytest.mark.asyncio
async def test_null_command_replies_not_understood():
    transport = _mock_transport(_ds_payload({"command": None, "reason": "분류 불가"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("랜덤 잡담")
    assert result is not None
    assert result.command is None
    assert result.reply == RESPONSE_NL_NOT_UNDERSTOOD


@pytest.mark.asyncio
async def test_api_error_replies_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("뭐 해줘")
    assert result is not None
    assert result.command is None
    assert result.reply == RESPONSE_NL_ERROR


@pytest.mark.asyncio
async def test_invalid_json_replies_error():
    transport = _mock_transport(_ds_payload("not json"))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("이상한 메시지")
    assert result is not None
    assert result.reply == RESPONSE_NL_ERROR


@pytest.mark.asyncio
async def test_classify_records_llm_call_with_usage():
    """LLM 호출 성공 시 redis 의 telegram_intent 키에 calls/tokens/cost 누적."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"command": "/balance", "args": ""})}}
                ],
                "usage": {"prompt_tokens": 220, "completion_tokens": 15},
            },
        )

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = IntentRouter(api_key="x", client=client, redis_client=redis)
        result = await router.classify("현금 얼마 있어?")

    assert result is not None
    assert (result.command, result.args) == ("/balance", "")
    keys = await redis.keys("llm:stats:*:telegram_intent")
    assert len(keys) == 1
    data = await redis.hgetall(keys[0])
    assert int(data["calls"]) == 1
    assert int(data["tokens_in"]) == 220
    assert int(data["tokens_out"]) == 15
    # DeepSeek chat: (220*0.27 + 15*1.10) / 1M = 75.9 / 1M = 0.0000759
    assert abs(float(data["cost_usd"]) - 0.0000759) < 1e-9


@pytest.mark.asyncio
async def test_classify_records_even_when_command_blocked():
    """위험 명령으로 거부되더라도 LLM 호출은 일어났으므로 stats 누적."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"command": "/buy", "args": "005930"})}}
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 20},
            },
        )

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = IntentRouter(api_key="x", client=client, redis_client=redis)
        result = await router.classify("삼성전자 사")

    assert result is not None
    assert result.command is None  # 안전 가드 — 안내 회신으로 대체
    keys = await redis.keys("llm:stats:*:telegram_intent")
    assert len(keys) == 1  # 그래도 호출 자체는 stats 에 들어감


@pytest.mark.asyncio
async def test_adopt_cancel_routed_with_confirm_stripped():
    """취소 의도 → /adopt cancel 라우팅. NL 경로의 "확인" 은 제거 — echo 까지만."""
    transport = _mock_transport(
        _ds_payload({"command": "/adopt", "args": "cancel 서진시스템 확인"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("서진시스템 조건부 매도 취소해줘")
    assert result is not None
    assert (result.command, result.args) == ("/adopt", "cancel 서진시스템")


@pytest.mark.asyncio
async def test_adopt_list_routed():
    transport = _mock_transport(_ds_payload({"command": "/adopt", "args": "list"}))
    async with httpx.AsyncClient(transport=transport) as client:
        router = IntentRouter(api_key="x", client=client)
        result = await router.classify("등록된 조건부 주문 보여줘")
    assert result is not None
    assert (result.command, result.args) == ("/adopt", "list")
