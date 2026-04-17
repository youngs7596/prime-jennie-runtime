"""파이프라인 E2E 테스트용 StubChatModel.

Scout/Macro LLM 호출을 Pydantic 고정 응답으로 대체. Docker/실 LLM 불필요.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from pydantic import BaseModel


class _StructuredHandle:
    def __init__(self, response: BaseModel) -> None:
        self._response = response

    async def ainvoke(self, messages: list[Any]) -> BaseModel:
        return self._response


class StubChatModel:
    """Pydantic 타입별 고정 응답을 반환하는 chat model."""

    def __init__(self, responses: dict[type, BaseModel]) -> None:
        self._responses = responses

    async def ainvoke(self, messages: list[Any]) -> Any:
        return AIMessage(content="stub")

    def bind_tools(self, tool_defs: list[Any]) -> StubChatModel:
        return self

    def with_structured_output(self, schema: type[BaseModel]) -> _StructuredHandle:
        if schema not in self._responses:
            raise KeyError(f"StubChatModel missing response for {schema.__name__}")
        return _StructuredHandle(self._responses[schema])
