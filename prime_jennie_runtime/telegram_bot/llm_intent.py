"""LLM 기반 자연어 → 명령 라우터 (v2 — 사람-승인 매매 설계 §3-3).

평문 한국어 ("오늘 손익?", "서진시스템 -1% 회복하면 매도") 를 알려진 슬래시
명령으로 변환. DeepSeek chat 모델 (env: DEEPSEEK_API_KEY / DEEPSEEK_MODEL) 사용.

설계:
- httpx 직접 호출 (langchain 의존 회피 — telegram-bot 이미지 가벼움 유지).
- response_format=json_object 로 강제 JSON. {"command": "/...", "args": "..."}
  또는 {"command": null, "reason": "..."} 형식.
- LLM 은 통역만 한다 — 매매가 실제로 나가는 길목 (확인 단어) 은 전부 슬래시
  직접 입력으로만 통과 가능. NL 경로로 추출된 args 의 "확인" 토큰은 여기서
  제거한다 ("/resume 확인" 류가 LLM 출력으로 실행되는 일 차단).
- 즉시 매매 명령 (/buy /sell /sellall /stop /liquidate) 은 추출돼도 실행하지
  않고 직접 입력 형식을 안내. 조건부 매도는 /adopt 로 라우팅 — /adopt 자체가
  2단계 확인이라 echo 까지만 간다.
- v1 의 무응답 설계 제거 (2026-06-10 전수조사: 사용자가 꺼짐/거부를 구분 못
  했음) — 분류 실패·거부 모두 안내를 회신한다. None 반환은 라우터 비활성
  (API key 없음) 일 때뿐.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from prime_jennie_runtime.dashboard.llm_pricing import calc_shadow_cost
from prime_jennie_runtime.infra.llm_stats import record_llm_call

logger = logging.getLogger(__name__)

# 안전한 명령 — LLM 추출 후 즉시 실행 가능. /adopt /accept /resume 은 자체
# 확인 단계가 있어 NL 라우팅이 echo/안내까지만 도달한다 (확인 토큰은 아래
# _strip_confirm_tokens 가 제거 — 실행은 직접 친 슬래시만).
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
    "/adopt",
    "/accept",
}
# 즉시 매매 명령 — LLM 추출하더라도 실행하지 않고 직접 입력 형식 안내만
_DANGEROUS_COMMANDS = {"/buy", "/sell", "/sellall", "/stop", "/liquidate"}

_DANGEROUS_USAGE = {
    "/buy": "/buy 종목 [수량]",
    "/sell": "/sell 종목 [수량|전량]",
    "/sellall": "/sellall 확인",
    "/stop": "/stop 확인",
    "/liquidate": "/liquidate add|remove|list|clear|arm|disarm|status",
}

RESPONSE_NL_NOT_UNDERSTOOD = (
    "무슨 명령인지 이해하지 못했습니다. <code>/help</code> 에서 명령 목록을 보거나,\n"
    '조건부 매도는 이렇게: "서진시스템 -1% 회복하면 매도해줘"'
)
RESPONSE_NL_ERROR = (
    "자연어 해석에 실패했습니다 (LLM 오류). 잠시 후 다시 시도하거나 슬래시 명령을 사용하세요."
)

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
- /adopt <종목> <회복선%> — 보유 종목의 조건부 매도 등록
- /adopt list — 등록된 조건부 주문 목록
- /adopt cancel <종목> — 등록된 조건부 주문 취소
- /accept <번호> — 오늘의 추천 수락 매수 (해석 확인 후 실행)
- /help — 도움말

조건부 매도 (/adopt) 규칙:
- "~가 양전하면 팔아줘", "손실이 -N% 위로 줄어들면 매도" 처럼 보유 종목을
  손익률 조건으로 매도하려는 의도면 /adopt 로 분류한다.
- args 는 "<종목> <회복선%>". 회복선은 -10 ~ 0 사이 숫자 하나 (% 기호 없이).
  "양전하면" 단독이면 0. 여러 조건이 "~하거나" 로 묶이면 가장 낮은(음수로 더
  깊은) 임계 하나만 쓴다.
- 예: "서진시스템이 양전하거나 -1% 미만으로 손실이 줄어들면 매도해줘"
  → {"command": "/adopt", "args": "서진시스템 -1"}
- 등록된 조건부 주문(조건부 매도)을 보여달라는 의도면
  → {"command": "/adopt", "args": "list"}
- 등록된 조건부 주문을 취소·해제하려는 의도면
  → {"command": "/adopt", "args": "cancel <종목>"}

추천 수락 (/accept) 규칙:
- "1번 사줘", "추천 2번 수락", "오늘 추천 첫 번째 매수해줘" 처럼 오늘의 추천을
  번호로 수락하려는 의도면 /accept 로 분류한다. args 는 번호 숫자 하나.
- 예: "1번 사줘" → {"command": "/accept", "args": "1"}
- 번호 없이 "추천 사줘" 면 command=null (어느 추천인지 모호).

출력 형식 (반드시 JSON 객체):
{"command": "/balance", "args": ""}
{"command": "/price", "args": "삼성전자"}
{"command": null, "reason": "분류 불가"}

규칙:
- 즉시 매수/즉시 매도/전량 청산/긴급정지/강제청산 요청이면 command=null
  (조건부 매도만 /adopt). 의도가 모호해도 command=null.
- args 는 명령 뒤에 붙는 인자만 (종목명, 가격, 분 등).
- JSON 외 다른 텍스트 출력 금지.
"""


@dataclass
class IntentResult:
    """classify 결과 — 라우팅할 명령 또는 사용자 안내 중 하나.

    command 가 있으면 라우팅, 없으면 reply 를 사용자에게 회신.
    """

    command: str | None = None
    args: str = ""
    reply: str | None = None


def _strip_confirm_tokens(args: str) -> str:
    """NL 경로로 새어 들어온 "확인" 토큰 제거.

    확인 단어는 사용자가 슬래시 명령으로 직접 입력해야만 유효하다 — LLM 출력이
    "/resume 확인" 을 만들어 kill switch 해제 같은 실행이 일어나는 일 차단.
    """
    return " ".join(t for t in args.split() if t != "확인")


class IntentRouter:
    """DeepSeek 기반 평문 → 명령 분류."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        redis_client: Any = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self._base_url = (
            base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        ).rstrip("/")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._redis_client = redis_client

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def classify(self, text: str) -> IntentResult | None:
        """평문 → IntentResult. None 은 라우터 비활성/빈 입력일 때뿐.

        실패·거부 경로는 전부 reply 가 채워진 IntentResult — 호출자는 그대로
        사용자에게 회신한다 (무응답 금지).
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
            return IntentResult(reply=RESPONSE_NL_ERROR)

        # LLM 호출 성공 — 분류 결과와 무관하게 stats 누적.
        usage_raw = data.get("usage") or {}
        in_tok = int(usage_raw.get("prompt_tokens", 0))
        out_tok = int(usage_raw.get("completion_tokens", 0))
        await record_llm_call(
            self._redis_client,
            service="telegram_intent",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=calc_shadow_cost(in_tok, out_tok),
        )

        cmd = parsed.get("command")
        if not cmd or not isinstance(cmd, str):
            return IntentResult(reply=RESPONSE_NL_NOT_UNDERSTOOD)
        cmd = cmd.lower().strip()
        if cmd in _DANGEROUS_COMMANDS:
            logger.info("intent router blocked dangerous command: %s", cmd)
            usage = _DANGEROUS_USAGE.get(cmd, cmd)
            return IntentResult(
                reply=(
                    "즉시 매매 명령은 직접 입력해야 실행됩니다: "
                    f"<code>{usage}</code>\n조건부 매도라면: <code>/adopt 종목 회복선%</code>"
                )
            )
        if cmd not in _SAFE_COMMANDS:
            return IntentResult(reply=RESPONSE_NL_NOT_UNDERSTOOD)
        args = _strip_confirm_tokens(str(parsed.get("args", "") or "").strip())
        return IntentResult(command=cmd, args=args)

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()


__all__ = [
    "RESPONSE_NL_ERROR",
    "RESPONSE_NL_NOT_UNDERSTOOD",
    "IntentResult",
    "IntentRouter",
]
