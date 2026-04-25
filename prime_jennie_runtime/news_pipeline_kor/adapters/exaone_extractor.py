"""EXAONE 4.0 event extraction adapter — LiteLLM 경유.

2026-04-21 설계 전환 (기존 exaone_sentiment 대체):
- 출력 형태: sentiment score 하나 → ``NewsEvent`` structured JSON
- event_type / impact_level / sentiment / time_horizon / keywords / sector_tags /
  financial_signals / confidence 를 한 번에 추출

Scout/Macro 가 RDB 조건식으로 소비 → 벡터 DB 의 semantic duplicate 문제 회피.

구조:
- ``LiteLLMEventExtractor`` — LiteLLM 호출. ``completion_fn`` 주입으로 테스트 가능.
- 실패 시 'other/low/neutral' NewsEvent 반환 (at-most-once 정책).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from ..models import (
    EventType,
    FinancialSignal,
    ImpactLevel,
    NewsArticle,
    NewsEvent,
    SentimentLabel,
    SignalDirection,
    TimeHorizon,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ollama/exaone3.5:32b"

_VALID_EVENT_TYPES: tuple[str, ...] = get_args(EventType)
_VALID_IMPACT: tuple[str, ...] = get_args(ImpactLevel)
_VALID_SENTIMENT: tuple[str, ...] = get_args(SentimentLabel)
_VALID_HORIZONS: tuple[str, ...] = get_args(TimeHorizon)
_VALID_DIRECTIONS: tuple[str, ...] = get_args(SignalDirection)

_PROMPT_TEMPLATE = """다음 한국 주식 뉴스에서 메타데이터를 JSON 으로 추출하세요.

종목코드: {ticker}
헤드라인: {title}
본문(발췌): {body_excerpt}

## event_type (아래 설명 참고, 가장 구체적인 것 선택. other 는 정말 맞는 게 없을 때만)
- earnings: 실적 발표 (매출/영업익/순익/어닝서프라이즈/분기 실적)
- mna: 인수·합병·매각·지분 거래·경영권
- lawsuit: 소송·고소·손해배상·판결·피소
- product: 신제품 출시·상용화·론칭·공개
- personnel: CEO/대표/임원 선임·사임·인사
- contract: 수주·계약·공급·납품 체결
- strike: 파업·노조·쟁의·단체협상
- shareholder_return: 배당·자사주 매입/소각·주주환원·밸류업
- investment: 유상증자·회사채·CB/BW·자금조달·투자유치
- bankruptcy: 상장폐지·워크아웃·기업회생·법정관리·파산
- market_movement: 지수 동향·코스피/코스닥 폭락·급등·시장심리·거래량
- geopolitical: 지정학 (이란·호르무즈·우크라이나·미중 무역·관세·전쟁)
- regulation: 정부 규제·승인·허가·심사·제재·정책 변화
- fund_product: ETF/펀드 신상품·순자산·운용사 뉴스·수익률 비교
- analyst_rating: 증권사 리포트·목표가·매수/매도 의견·컨센서스
- other: 위 15개 중 어디에도 명확히 속하지 않는 경우만

## 필드 정의
- impact_level: {impact_levels} 중 하나 (주가 영향 크기). **반드시 아래 기준에 따라 보수적으로**:
  - high: 당일 ±5% 이상 가격 변동을 합리적으로 예상할 수 있는 사건만.
    예) 어닝서프라이즈/쇼크, 상장폐지·법정관리, M&A 본 거래·지분 매각, 대형 수주(매출 대비 10%+),
    임상 3상 결과, FDA 승인, 8% 이상 유상증자, 정부의 직접적 영업제한 규제,
    호르무즈/대만/대규모 전쟁 같은 즉시 시장 충격 지정학.
  - medium: 당일 ±1~5% 변동 예상. 정황상 영향이 분명하지만 한 분기 안에 흡수될 수준.
    예) 증권사 목표가 변경/투자의견 조정, 부분파업/공급차질, 신상품 출시,
    임원 인사·차기 회장 후보 부상, 코스피 일일 1~3% 등락, ETF 순자산 이슈,
    중소형 수주, 소송 1심 결과(상고 예정), 행정지도/지침 수준 규제.
  - low: 사실 기록 정도. 주가 영향 미미하거나 이미 반영된 정보.
    예) 정기 IR 일정 공지, 임원 정기 승진, 일반적 시황·동향 코멘트,
    증권사 단순 커버리지 개시, 컨센서스 부합 실적, 분기보고서 제출 사실 자체.
  **판정 가이드**: 헤드라인이 충격성 표현("어닝쇼크", "폭락", "사상최대", "법정관리")을
  쓰지 않고 사건 규모(액수·비중·시한)도 명시되지 않았다면 medium 이하로 내린다.
  `매출 대비 10% 미만 수주`, `목표가 ±20% 미만 변경`, `노조 부분파업`, `1심 판결`
  같은 케이스는 high 가 아니다.
- sentiment: {sentiments} 중 하나
- sentiment_score: -1.0~+1.0 (0=중립)
- time_horizon: {horizons} 중 하나
  (immediate=당일, short=1주 이내, medium=1개월 이내, long=분기 이상)
- keywords: 한국어 키워드 3~5개 배열 (고유명사 원형 유지, 숫자/영문 혼합 가능)
- sector_tags: 관련 섹터 태그 0~3개 (예: 반도체/자동차/제약/금융/에너지)
- financial_signals: [{{"type":"revenue|profit|guidance|dividend|margin",
  "direction":"up|down|flat"}}, ...] 0~3개
- confidence: 0.0~1.0 분류 확신도

응답은 JSON 오브젝트로만. 다른 텍스트 금지.

## 예시 1 (실적)
입력: "삼성전자, 1분기 영업이익 7조 어닝서프라이즈… HBM 수요 폭발"
출력: {{"event_type":"earnings","impact_level":"high","sentiment":"positive",
"sentiment_score":0.85,"time_horizon":"short",
"keywords":["삼성전자","영업이익","HBM","어닝서프라이즈"],
"sector_tags":["반도체"],
"financial_signals":[{{"type":"revenue","direction":"up"}},
{{"type":"profit","direction":"up"}}],"confidence":0.95}}

## 예시 2 (지정학)
입력: "이란, 호르무즈 해협 봉쇄 경고에 외국인 8조원 이탈"
출력: {{"event_type":"geopolitical","impact_level":"high","sentiment":"negative",
"sentiment_score":-0.7,"time_horizon":"immediate",
"keywords":["이란","호르무즈","봉쇄","외국인"],
"sector_tags":["에너지"],
"financial_signals":[],"confidence":0.9}}

## 예시 3 (ETF/펀드)
입력: "타임폴리오 액티브 ETF 순자산 2조 돌파… 배당주 펀드 유입"
출력: {{"event_type":"fund_product","impact_level":"medium","sentiment":"positive",
"sentiment_score":0.4,"time_horizon":"medium",
"keywords":["타임폴리오","액티브 ETF","순자산","배당주"],
"sector_tags":["금융"],
"financial_signals":[{{"type":"dividend","direction":"up"}}],"confidence":0.85}}

## 예시 4 (medium — 증권사 단순 목표가 변경)
입력: "한국투자증권, LG화학 목표주가 38만원 → 42만원 소폭 상향"
출력: {{"event_type":"analyst_rating","impact_level":"medium","sentiment":"positive",
"sentiment_score":0.3,"time_horizon":"medium",
"keywords":["LG화학","한국투자증권","목표가","상향"],
"sector_tags":["화학"],
"financial_signals":[],"confidence":0.7}}

## 예시 5 (low — 정기 IR 일정 사실 보도)
입력: "한화에어로스페이스, 5월 14일 1분기 실적발표 컨퍼런스콜 개최 예정"
출력: {{"event_type":"earnings","impact_level":"low","sentiment":"neutral",
"sentiment_score":0.0,"time_horizon":"short",
"keywords":["한화에어로스페이스","컨퍼런스콜","실적발표"],
"sector_tags":["방산"],
"financial_signals":[],"confidence":0.8}}"""

CompletionFn = Callable[..., Awaitable[Any]]


@dataclass
class LiteLLMEventExtractor:
    """EXAONE structured JSON 추출 ``EventExtractor``."""

    model: str = DEFAULT_MODEL
    api_base: str | None = None
    completion_fn: CompletionFn | None = None
    body_excerpt_chars: int = 400
    model_name_override: str | None = None
    temperature: float = 0.1
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    async def extract(self, article: NewsArticle) -> NewsEvent:
        prompt = _PROMPT_TEMPLATE.format(
            ticker=article.ticker,
            title=article.title,
            body_excerpt=article.body[: self.body_excerpt_chars] or "(본문 없음)",
            impact_levels="/".join(_VALID_IMPACT),
            sentiments="/".join(_VALID_SENTIMENT),
            horizons="/".join(_VALID_HORIZONS),
        )

        raw = await self._call_llm(prompt)
        parsed = _parse_event_json(raw)
        if parsed is None:
            logger.warning(
                "exaone extractor parse failed: article=%s raw=%s",
                article.article_id,
                (raw or "")[:200],
            )
            return self._fallback(article)

        return _build_event(article, parsed, model=self.model_name_override or self.model)

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
            logger.exception("exaone extractor completion failed")
            return ""
        return _extract_content(response)

    def _fallback(self, article: NewsArticle) -> NewsEvent:
        return NewsEvent(
            article_id=article.article_id,
            ticker=article.ticker,
            published_at=article.published_at,
            event_type="other",
            impact_level="low",
            sentiment="neutral",
            sentiment_score=0.0,
            time_horizon="short",
            keywords=[],
            sector_tags=[],
            financial_signals=[],
            confidence=0.0,
            model=self.model_name_override or self.model,
            analyzed_at=datetime.now(),
        )


# ---------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------


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


def _parse_event_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = raw.replace("```json", "").replace("```", "").strip()

    candidates = [cleaned]
    match = _JSON_OBJ_RE.search(cleaned)
    if match:
        candidates.append(match.group(0))

    for s in candidates:
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _coerce_enum(value: Any, valid: tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value in valid:
        return value
    return default


def _coerce_float(value: Any, *, lo: float, hi: float, default: float) -> float:
    if isinstance(value, int | float):
        return max(lo, min(hi, float(value)))
    return default


def _coerce_keyword_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def _coerce_signals(value: Any) -> list[FinancialSignal]:
    if not isinstance(value, list):
        return []
    out: list[FinancialSignal] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        sig_type = item.get("type")
        direction = item.get("direction")
        if not isinstance(sig_type, str) or not isinstance(direction, str):
            continue
        if direction not in _VALID_DIRECTIONS:
            continue
        out.append(FinancialSignal(type=sig_type.strip()[:40], direction=direction))  # type: ignore[arg-type]
        if len(out) >= 5:
            break
    return out


def _build_event(article: NewsArticle, parsed: dict, *, model: str) -> NewsEvent:
    return NewsEvent(
        article_id=article.article_id,
        ticker=article.ticker,
        published_at=article.published_at,
        event_type=_coerce_enum(parsed.get("event_type"), _VALID_EVENT_TYPES, "other"),  # type: ignore[arg-type]
        impact_level=_coerce_enum(parsed.get("impact_level"), _VALID_IMPACT, "low"),  # type: ignore[arg-type]
        sentiment=_coerce_enum(parsed.get("sentiment"), _VALID_SENTIMENT, "neutral"),  # type: ignore[arg-type]
        sentiment_score=_coerce_float(parsed.get("sentiment_score"), lo=-1.0, hi=1.0, default=0.0),
        time_horizon=_coerce_enum(parsed.get("time_horizon"), _VALID_HORIZONS, "short"),  # type: ignore[arg-type]
        keywords=_coerce_keyword_list(parsed.get("keywords"), limit=10),
        sector_tags=_coerce_keyword_list(parsed.get("sector_tags"), limit=5),
        financial_signals=_coerce_signals(parsed.get("financial_signals")),
        confidence=_coerce_float(parsed.get("confidence"), lo=0.0, hi=1.0, default=0.5),
        model=model,
        analyzed_at=datetime.now(),
    )


__all__ = ["DEFAULT_MODEL", "LiteLLMEventExtractor"]
