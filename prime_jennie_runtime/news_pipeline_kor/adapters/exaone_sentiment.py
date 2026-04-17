"""EXAONE 4.0 sentiment adapter — LiteLLM 경유.

v2 ``prime_jennie/services/news/analyzer.py`` (290줄) 의 프롬프트 + JSON 파서 +
점수 범위 핵심 로직을 v3 ``SentimentAnalyzer`` Protocol 구현체로 포팅.

v2 ↔ v3 차이:
- v2 score: 0~100 (50=중립). v3 score: -1.0~+1.0 (0=중립).
  변환: `v3 = (v2 - 50) / 50`
- v2 는 ``BaseLLMProvider.generate_json`` (자체 어댑터). v3 는 ``litellm.acompletion``.
- v2 배치(asyncio.gather)는 호출자 책임 — analyzer 는 단건 분석만.

구조:
- ``LiteLLMSentimentAnalyzer`` — LiteLLM 호출. ``completion_fn`` 주입으로 테스트 가능.
- LLM 호출 실패 또는 파싱 실패 시 neutral(score=0.0, label='neutral') 반환.
- 모델 기본값은 ``infra.config.LLMConfig.fast`` 티어와 동일 (``ollama/exaone3.5:32b``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models import NewsArticle, SentimentScore
from ..sentiment import _label_for  # label 이산화 규칙 재사용 (±0.2)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ollama/exaone3.5:32b"

_PROMPT_TEMPLATE = (
    "다음 한국 주식 뉴스의 감성을 분석하세요.\n"
    "종목코드: {ticker}\n"
    "헤드라인: {title}\n"
    "본문(발췌): {body_excerpt}\n\n"
    "규칙:\n"
    "- score: 0~100 정수 (50=중립, 높을수록 긍정)\n"
    "- reason: 한국어 1문장\n"
    "- 응답은 JSON 오브젝트로만. 다른 텍스트 금지.\n"
    '- 예시: {{"score": 72, "reason": "호실적 발표"}}'
)

# LiteLLM completion_fn 시그니처 — (model, messages, **kwargs) → Awaitable[dict]
CompletionFn = Callable[..., Awaitable[Any]]


@dataclass
class LiteLLMSentimentAnalyzer:
    """EXAONE (또는 호환 모델) 을 경유하는 ``SentimentAnalyzer``.

    - ``model``: LiteLLM model 문자열. 기본값은 EXAONE local Ollama.
    - ``api_base``: Ollama 서버 URL 등. None 이면 LiteLLM 기본값.
    - ``completion_fn``: 주입 가능. None 이면 ``litellm.acompletion`` 지연 import.
    - ``body_excerpt_chars``: 프롬프트에 넣을 본문 길이 상한. 300자 기본.
    """

    model: str = DEFAULT_MODEL
    api_base: str | None = None
    completion_fn: CompletionFn | None = None
    body_excerpt_chars: int = 300
    model_name_override: str | None = None
    temperature: float = 0.1
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    async def analyze(self, article: NewsArticle) -> SentimentScore:
        prompt = _PROMPT_TEMPLATE.format(
            ticker=article.ticker,
            title=article.title,
            body_excerpt=article.body[: self.body_excerpt_chars] or "(본문 없음)",
        )

        raw = await self._call_llm(prompt)
        score_v2 = _parse_score(raw)
        if score_v2 is None:
            logger.warning(
                "exaone sentiment parse failed: article=%s raw=%s",
                article.article_id,
                (raw or "")[:120],
            )
            return self._neutral(article)

        score_v3 = _to_v3_score(score_v2)
        return SentimentScore(
            article_id=article.article_id,
            ticker=article.ticker,
            score=score_v3,
            label=_label_for(score_v3),
            analyzed_at=datetime.now(),
            model=self.model_name_override or self.model,
        )

    async def _call_llm(self, prompt: str) -> str:
        fn = self.completion_fn or await _default_completion_fn()
        try:
            response = await fn(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                api_base=self.api_base,
                temperature=self.temperature,
                **self.extra_kwargs,
            )
        except Exception:
            logger.exception("exaone sentiment completion failed")
            return ""
        return _extract_content(response)

    def _neutral(self, article: NewsArticle) -> SentimentScore:
        return SentimentScore(
            article_id=article.article_id,
            ticker=article.ticker,
            score=0.0,
            label="neutral",
            analyzed_at=datetime.now(),
            model=self.model_name_override or self.model,
        )


# ---------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------


async def _default_completion_fn() -> CompletionFn:
    """지연 import — litellm 이 heavy 의존성이므로 런타임에만 로드."""
    import litellm  # type: ignore[import-untyped]

    return litellm.acompletion  # type: ignore[return-value]


def _extract_content(response: Any) -> str:
    """LiteLLM / OpenAI-shape 응답에서 첫 choice 의 content 를 꺼낸다."""
    try:
        choices = response["choices"] if isinstance(response, dict) else response.choices
        first = choices[0]
        message = first["message"] if isinstance(first, dict) else first.message
        content = message["content"] if isinstance(message, dict) else message.content
        return content or ""
    except (KeyError, IndexError, AttributeError):
        return ""


_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_score(raw: str) -> int | None:
    """문자열 응답에서 JSON 오브젝트를 뽑아 score(int) 를 반환.

    LLM 은 종종 코드펜스(```json ... ```) 로 감싸서 응답 → regex 로 느슨히 추출.
    """
    if not raw:
        return None
    # 코드펜스 제거
    cleaned = raw.replace("```json", "").replace("```", "").strip()

    candidates = [cleaned]
    # 첫 번째 JSON 오브젝트 매치도 시도 (text 중간에 섞여 있을 때)
    match = _JSON_OBJ_RE.search(cleaned)
    if match:
        candidates.append(match.group(0))

    for s in candidates:
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        score = obj.get("score")
        if isinstance(score, int | float):
            return max(0, min(100, int(score)))
    return None


def _to_v3_score(v2_score: int) -> float:
    """0~100 → -1.0~+1.0 (50=0)."""
    return max(-1.0, min(1.0, (v2_score - 50) / 50.0))


__all__ = ["DEFAULT_MODEL", "LiteLLMSentimentAnalyzer"]
