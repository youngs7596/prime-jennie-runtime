"""일일 digest summary 생성.

- 기본 경로: DeepSeek chat (V3.2) 로 영문 본문을 한국어 매크로 요약으로 압축
  · Macro prompt (slow_loop) 도 한국어라 일관성 유지
  · ~$0.001/호출 비용
- 실패/키 없을 때: headlines 기반 포뮬러 fallback. Macro 게이트 입력은
  unavailable 보다는 factual digest 가 낫다.

httpx 직접 호출 — job-worker 에 langchain 의존성을 추가하지 않기 위해서.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .models import GlobalNewsArticle

logger = logging.getLogger(__name__)


_SUMMARIZER_SYSTEM = (
    "너는 한국 주식 매크로 리서치 어시스턴트다. WSJ/Bloomberg/Reuters 헤드라인 목록을 "
    "한국어로 6~10줄 이내로 요약해라. 반드시 시장에 영향 가능한 매크로 이벤트 "
    "(금리, 연준, 지정학, 환율, 원자재, 주요 기업 실적) 중심. 근거 없는 예측 금지."
)


def _fallback_summary(articles: list[GlobalNewsArticle]) -> str:
    """LLM 없이 만드는 요약. 소스별 건수 + 가장 최근 헤드라인 3건."""
    if not articles:
        return "(최근 24h 해외 매크로 뉴스 없음)"
    by_source: dict[str, int] = {}
    for a in articles:
        by_source[a.source] = by_source.get(a.source, 0) + 1
    source_line = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
    top3 = articles[:3]
    bullets = "\n".join(f"- [{a.source}] {a.title}" for a in top3)
    return f"최근 24h 해외 매크로 뉴스 {len(articles)}건 ({source_line}).\n{bullets}"


def _build_prompt(articles: list[GlobalNewsArticle]) -> str:
    lines = []
    for a in articles[:30]:
        desc = (a.description or "").replace("\n", " ")[:200]
        lines.append(f"- [{a.source}] {a.title} — {desc}")
    return "다음 해외 매크로 헤드라인을 한국 주식 시장 관점에서 6-10줄로 요약:\n\n" + "\n".join(
        lines
    )


async def summarize(
    articles: list[GlobalNewsArticle],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str | None, dict[str, int] | None]:
    """(summary, model_used, usage) — usage 는 {"input_tokens", "output_tokens"}.

    실패/fallback 경로에서는 model_used=None, usage=None 으로 회신. caller 는
    model_used 가 set 일 때만 record_llm_call 을 부르면 된다.
    """
    if not articles:
        return _fallback_summary(articles), None, None

    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    base_url = (
        base_url or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
    ).rstrip("/")
    model = (
        model
        or os.environ.get("MACRO_DIGEST_SUMMARIZER_MODEL")
        or os.environ.get("DEEPSEEK_MODEL")
        or "deepseek-chat"
    )

    if not api_key:
        logger.info("digest summarizer: no DEEPSEEK_API_KEY, using fallback")
        return _fallback_summary(articles), None, None

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUMMARIZER_SYSTEM},
            {"role": "user", "content": _build_prompt(articles)},
        ],
        "temperature": 0.2,
        "max_tokens": 800,
    }
    own = http_client is None
    if own:
        http_client = httpx.AsyncClient(timeout=60.0)
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        if not content:
            return _fallback_summary(articles), None, None
        usage_raw = data.get("usage") or {}
        usage = {
            "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
            "output_tokens": int(usage_raw.get("completion_tokens", 0)),
        }
        return content, model, usage
    except Exception as exc:  # 네트워크/키/스키마 모두 여기서 잡히고 fallback
        logger.warning("digest summarizer failed: %s", exc)
        return _fallback_summary(articles), None, None
    finally:
        if own:
            await http_client.aclose()  # type: ignore[union-attr]
