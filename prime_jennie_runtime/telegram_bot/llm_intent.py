"""LLM 기반 자연어 → 명령 라우터.

평문 한국어 ("오늘 손익?", "삼성전자 가격 알려줘") 를 알려진 슬래시 명령으로
변환. DeepSeek chat 모델 (env: DEEPSEEK_API_KEY / DEEPSEEK_MODEL) 사용.

설계:
- httpx 직접 호출 (langchain 의존 회피 — telegram-bot 이미지 가벼움 유지).
- response_format=json_object 로 강제 JSON. {"command": "/...", "args": "..."}
  또는 {"command": null, "reason": "..."} 형식.
- API key 없거나 호출 실패 시 None 반환 → bot 은 평소대로 무응답 (현재 행동
  보존).
- 매매성 명령 (`/buy /sell /sellall /stop`, `/liquidate arm`) 은 LLM 이 추출
  하더라도 직접 실행하지 않고 사용자에게 슬래시 명령 형식 가이드만 회신.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# 안전한 명령 — LLM 추출 후 즉시 실행 가능
_SAFE_COMMANDS = {
    "/help",
    "/status",
    "/portfolio",
    "/pnl",
    "/balance",
    "/price",
    "/watchlist",
    "/watch",
    "/unwatch",
    "/alerts",
    "/alert",
    "/mute",
    "/unmute",
    "/maxbuy",
    "/config",
    "/pause",
    "/resume",
    "/dryrun",
    "/diagnose",
}
# 위험 명령 — LLM 추출하더라도 즉시 실행 금지 (사용자가 슬래시로 직접 입력해야)
_DANGEROUS_COMMANDS = {"/buy", "/sell", "/sellall", "/stop", "/liquidate"}

_SYSTEM_PROMPT = """너는 한국어 평문 메시지를 텔레그램 슬래시 명령으로 분류한다.

알려진 명령:
- /status — 시스템 상태
- /portfolio — 보유 종목
- /pnl — 오늘 손익
- /balance — 현금 잔고
- /price <종목> — 현재가
- /watchlist — 워치리스트 목록
- /watch <종목> — 추가
- /unwatch <종목> — 제거
- /alerts — 가격 알림 목록
- /alert <종목> <가격> — 가격 알림 설정
- /mute <분> — 알림 음소거
- /unmute — 음소거 해제
- /maxbuy <N> — 일일 최대 매수
- /config — 설정 조회
- /pause [사유] — 일시정지
- /resume — 재개
- /dryrun on|off — 시뮬레이션
- /diagnose — 시스템 진단
- /help — 도움말

출력 형식 (반드시 JSON 객체):
{"command": "/balance", "args": ""}
{"command": "/price", "args": "삼성전자"}
{"command": null, "reason": "분류 불가"}

규칙:
- 의도가 모호하거나 매매(매수/매도/긴급정지/강제청산) 요청이면 command=null
- args 는 명령 뒤에 붙는 인자만 (종목명, 가격, 분 등)
- JSON 외 다른 텍스트 출력 금지
"""


class IntentRouter:
    """DeepSeek 기반 평문 → 명령 분류."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self._base_url = (
            base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        ).rstrip("/")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15.0)

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def classify(self, text: str) -> tuple[str, str] | None:
        """평문 → ``(command, args)`` 또는 None.

        매매성 명령은 의도가 명확해도 None 반환 (안전 가드).
        """
        if not self._api_key:
            return None
        text = text.strip()
        if not text:
            return None

        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0,
                    "max_tokens": 100,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception:
            logger.exception("intent classification failed")
            return None

        cmd = parsed.get("command")
        if not cmd or not isinstance(cmd, str):
            return None
        cmd = cmd.lower().strip()
        if cmd in _DANGEROUS_COMMANDS:
            logger.info("intent router blocked dangerous command: %s", cmd)
            return None
        if cmd not in _SAFE_COMMANDS:
            return None
        args = parsed.get("args", "") or ""
        return cmd, str(args).strip()

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()


__all__ = ["IntentRouter"]
