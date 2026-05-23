"""News Agent (Phase 1 shadow) — LLM-at-core ticker 매핑.

design `.ai/designs/2026-05-23-news-agent.md` §6 Phase 1.

본 모듈은 기사 본문 → 진짜 관련 ticker(s) 식별 + market_general 여부 판정.
Pre-flight v2 prompt (검증 완료: Metric 1 0.989) 를 정식화. 결과는
``NewsEvent.shadow_metadata`` 에 저장되어 운영 path 와 병행 (운영 영향 0).

Phase 2 진입 시 본 클래스가 정식 NewsAgent 로 승격되고 `news_event_tickers`
테이블에 직접 emit.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import NewsArticle

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ollama/exaone3.5:32b"  # 운영 override (vLLM Qwen3)

_PROMPT_TEMPLATE = """다음 한국 주식 뉴스의 진짜 주제가 어떤 한국 상장사인지 식별하세요.

헤드라인: {title}
본문: {body}

## 작업
이 기사가 **직접 다루는** 한국 상장사 0~5개. 단순 언급이 아니라 "기사 주제" 인 회사만.

## 절대 ticker 부여 금지 케이스 (tickers=[] + is_market_general=true)
- 코스피/코스닥/지수 일반 동향 (예: "코스피 600p 반등", "외국인 매도", "8000선 도전")
- 환율/금리/유가 시황 (예: "달러 1350원", "WTI 80달러")
- 시장 분류 일반 뉴스 (예: "대구 상장법인 1분기 영업이익", "은행권 전체 동향")
- 해외 회사 단독 주제 (엔비디아·테슬라·애플 등 — 한국 상장사 영향 명시 안 됐으면)
- 정책·규제 일반 (예: "공정위 과징금", "정부 ETF 규제")

## ticker 부여 조건
- 기사 제목 또는 본문에 회사의 정식명/약어/별칭이 명시적 등장
- 그 회사의 사건이 기사 핵심 주제 (CEO 인사, 실적, M&A, 신제품, 수주, 소송 등)

## hallucination 금지 — 가공의 회사명 금지
- 존재하지 않는 종목명 만들지 마세요 (예: "삼성제삼", "대구상장법인")
- 약어가 애매하면 부여 안 함이 안전 (예: "삼성"만으론 삼성전자/삼성물산/삼성SDI 등 불명)
- 본문에 풀명이 나오면 disambiguate. 풀명 없으면 tickers=[]

## 출력 (JSON object only)
{{
  "tickers": ["삼성물산", "..."],
  "ticker_rationales": {{
    "삼성물산": "삼성물산의 압구정4구역 시공권 획득이 주제"
  }},
  "is_market_general": false,
  "confidence": 0.9
}}

- tickers: 한국 상장사 정식 이름 (한글). KOSPI/KOSDAQ 실존 종목만
- is_market_general: tickers 가 비어있고 일반 시장/지수/환율/정책 뉴스이면 true
- confidence: 0.0~1.0

## 예시
입력: "코스피 7200선 마감, 외국인 6조 순매도"
출력: {{"tickers": [], "ticker_rationales": {{}},
       "is_market_general": true, "confidence": 0.95}}

입력: "삼성 노조 부문 70·사업부 30 고집" (본문: 삼성전자 노사 협의)
출력: {{"tickers": ["삼성전자"],
       "ticker_rationales": {{"삼성전자": "노사 협상 주제"}},
       "is_market_general": false, "confidence": 0.85}}

입력: "삼전·하닉 프리마켓 5%대 급등"
출력: {{"tickers": ["삼성전자", "SK하이닉스"],
       "ticker_rationales": {{"삼성전자": "삼전 약어",
                              "SK하이닉스": "하닉 약어"}},
       "is_market_general": false, "confidence": 0.9}}
"""

CompletionFn = Callable[..., Awaitable[Any]]


@dataclass
class ShadowResult:
    tickers: list[str]
    ticker_rationales: dict[str, str]
    is_market_general: bool
    confidence: float
    model: str
    analyzed_at: datetime

    def to_metadata(self) -> dict:
        return {
            "tickers": self.tickers,
            "ticker_rationales": self.ticker_rationales,
            "is_market_general": self.is_market_general,
            "confidence": self.confidence,
            "model": self.model,
            "analyzed_at": self.analyzed_at.isoformat(),
        }


@dataclass
class NewsAgentShadow:
    """Phase 1 shadow NewsAgent — Qwen3 vLLM 호출. ``completion_fn`` 주입으로 테스트."""

    model: str = DEFAULT_MODEL
    api_base: str | None = None
    completion_fn: CompletionFn | None = None
    temperature: float = 0.1
    timeout_s: float = 60.0
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    async def extract(self, article: NewsArticle) -> ShadowResult | None:
        prompt = _PROMPT_TEMPLATE.format(title=article.title, body=article.body or "(본문 없음)")
        raw = await self._call_llm(prompt)
        parsed = _parse_json(raw)
        if parsed is None:
            logger.warning(
                "shadow agent parse failed article=%s raw=%s",
                article.article_id,
                (raw or "")[:200],
            )
            return None
        return _build_result(parsed, model=self.model)

    async def _call_llm(self, prompt: str) -> str:
        fn = self.completion_fn or await _default_completion_fn()
        try:
            response = await fn(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                api_base=self.api_base,
                temperature=self.temperature,
                timeout=self.timeout_s,
                **self.extra_kwargs,
            )
        except Exception:
            logger.exception("shadow agent completion failed")
            return ""
        return _extract_content(response)


async def _default_completion_fn() -> CompletionFn:
    import litellm  # type: ignore[import-untyped]

    return litellm.acompletion  # type: ignore[return-value]


def _extract_content(response: Any) -> str:
    try:
        choices = response["choices"] if isinstance(response, dict) else response.choices
        first = choices[0]
        message = first["message"] if isinstance(first, dict) else first.message
        content = message["content"] if isinstance(message, dict) else message.content
        return content or ""
    except (KeyError, IndexError, AttributeError):
        return ""


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    candidates = [cleaned]
    m = _JSON_OBJ_RE.search(cleaned)
    if m:
        candidates.append(m.group(0))
    for s in candidates:
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _coerce_str_list(value: Any, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def _coerce_rationales(value: Any, allowed: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    allowed_set = set(allowed)
    for k, v in value.items():
        if isinstance(k, str) and k in allowed_set and isinstance(v, str):
            out[k] = v.strip()[:200]
    return out


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _coerce_float(value: Any, *, lo: float, hi: float, default: float) -> float:
    if isinstance(value, int | float):
        return max(lo, min(hi, float(value)))
    return default


def _build_result(parsed: dict, *, model: str) -> ShadowResult:
    tickers = _coerce_str_list(parsed.get("tickers"), limit=5)
    return ShadowResult(
        tickers=tickers,
        ticker_rationales=_coerce_rationales(parsed.get("ticker_rationales"), tickers),
        is_market_general=_coerce_bool(parsed.get("is_market_general"), not tickers),
        confidence=_coerce_float(parsed.get("confidence"), lo=0.0, hi=1.0, default=0.5),
        model=model,
        analyzed_at=datetime.now(),
    )


__all__ = ["DEFAULT_MODEL", "NewsAgentShadow", "ShadowResult"]
