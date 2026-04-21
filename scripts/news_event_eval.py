"""News event extractor eval harness.

LLM 교체·프롬프트 튜닝의 회귀를 잡는 기준선. fixture 한 건씩
``LiteLLMEventExtractor.extract`` 에 태우고 `expected` 와 비교해 메트릭 출력.

지표
----

- schema_valid_rate  — 출력이 NewsEvent 로 파싱 성공한 비율 (fallback 포함 — 실무상 100% 권장)
- event_type_acc     — `expected.event_type` 일치율
- impact_acc         — `expected.impact_level` 일치율
- sentiment_acc      — `expected.sentiment` (positive/neutral/negative) 일치율
- sentiment_sign_acc — `expected.sentiment_score_sign` (positive/negative) 과 실 score 부호 일치
- keyword_hit_rate   — `expected.keywords_must_include` 의 각 키워드가 실제 keywords 에 포함된 비율
                       (한국어 고유명사 보존 측정의 1차 근사)
- sector_hit_rate    — `expected.sector_tags_must_include` 포함 비율
- per-article JSON  — `--json-out` 지정 시 개별 결과 저장 (regression 비교용)

사용
----

로컬 vLLM (MS-01 tunnel 또는 직접 연결) 기준:

    uv run python scripts/news_event_eval.py \\
        --fixture tests/fixtures/news_eval/baseline.json \\
        --model hosted_vllm/jart25/Qwen3-30B-A3B-Instruct-2507-Autoround-Int-4bit-gptq \\
        --api-base http://127.0.0.1:8001/v1 \\
        --json-out reports/eval-$(date +%Y%m%d-%H%M).json

"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from prime_jennie_runtime.news_pipeline_kor.adapters.exaone_extractor import (
    LiteLLMEventExtractor,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle, NewsEvent


@dataclass
class CaseResult:
    article_id: str
    title: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    hits: dict[str, bool | float]


def _load_fixtures(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"fixture must be a JSON array, got {type(data).__name__}")
    return data


def _to_article(item: dict) -> NewsArticle:
    published = item["published_at"]
    published_dt = datetime.fromisoformat(published) if isinstance(published, str) else published
    return NewsArticle(
        article_id=item["article_id"],
        ticker=item["ticker"],
        title=item["title"],
        body=item.get("body", ""),
        published_at=published_dt,
        source_url=item["source_url"],
        source_name=item.get("source_name", ""),
    )


def _score_sign(score: float) -> str:
    if score > 0.1:
        return "positive"
    if score < -0.1:
        return "negative"
    return "neutral"


def _grade(expected: dict, event: NewsEvent) -> dict[str, bool | float]:
    hits: dict[str, bool | float] = {}

    hits["event_type_match"] = event.event_type == expected.get("event_type")
    hits["impact_match"] = event.impact_level == expected.get("impact_level")
    if "sentiment" in expected:
        hits["sentiment_match"] = event.sentiment == expected["sentiment"]
    if "sentiment_score_sign" in expected:
        hits["sentiment_sign_match"] = (
            _score_sign(event.sentiment_score) == expected["sentiment_score_sign"]
        )

    kws_expected = expected.get("keywords_must_include", [])
    if kws_expected:
        produced = {k.lower() for k in event.keywords}
        hit = sum(1 for k in kws_expected if k.lower() in produced)
        hits["keyword_hit_rate"] = hit / len(kws_expected)

    sectors_expected = expected.get("sector_tags_must_include", [])
    if sectors_expected:
        produced_sec = {s.lower() for s in event.sector_tags}
        hit = sum(1 for s in sectors_expected if s.lower() in produced_sec)
        hits["sector_hit_rate"] = hit / len(sectors_expected)

    return hits


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _aggregate(results: list[CaseResult]) -> dict[str, Any]:
    n = len(results)
    event_hits = sum(1 for r in results if r.hits.get("event_type_match") is True)
    impact_hits = sum(1 for r in results if r.hits.get("impact_match") is True)

    sent_cases = [r for r in results if "sentiment_match" in r.hits]
    sent_hits = sum(1 for r in sent_cases if r.hits["sentiment_match"] is True)

    sign_cases = [r for r in results if "sentiment_sign_match" in r.hits]
    sign_hits = sum(1 for r in sign_cases if r.hits["sentiment_sign_match"] is True)

    kw_rates = [float(r.hits["keyword_hit_rate"]) for r in results if "keyword_hit_rate" in r.hits]
    sector_rates = [
        float(r.hits["sector_hit_rate"]) for r in results if "sector_hit_rate" in r.hits
    ]

    schema_valid = sum(1 for r in results if r.actual.get("event_type") != "__extract_failed__")

    return {
        "n": n,
        "schema_valid_rate": schema_valid / n if n else 0.0,
        "event_type_acc": event_hits / n if n else 0.0,
        "impact_acc": impact_hits / n if n else 0.0,
        "sentiment_acc": sent_hits / len(sent_cases) if sent_cases else None,
        "sentiment_sign_acc": sign_hits / len(sign_cases) if sign_cases else None,
        "keyword_hit_rate": _mean(kw_rates) if kw_rates else None,
        "sector_hit_rate": _mean(sector_rates) if sector_rates else None,
    }


def _print_report(results: list[CaseResult], agg: dict[str, Any]) -> None:
    print(f"\n=== News Event Eval — {agg['n']} cases ===")
    print(f"schema_valid_rate   : {agg['schema_valid_rate']:.1%}")
    print(f"event_type_acc      : {agg['event_type_acc']:.1%}")
    print(f"impact_acc          : {agg['impact_acc']:.1%}")
    if agg["sentiment_acc"] is not None:
        print(f"sentiment_acc       : {agg['sentiment_acc']:.1%}")
    if agg["sentiment_sign_acc"] is not None:
        print(f"sentiment_sign_acc  : {agg['sentiment_sign_acc']:.1%}")
    if agg["keyword_hit_rate"] is not None:
        print(f"keyword_hit_rate    : {agg['keyword_hit_rate']:.1%}")
    if agg["sector_hit_rate"] is not None:
        print(f"sector_hit_rate     : {agg['sector_hit_rate']:.1%}")

    print("\n--- 실패 케이스 ---")
    any_fail = False
    for r in results:
        fails = [k for k, v in r.hits.items() if v is False or (isinstance(v, float) and v < 1.0)]
        if not fails:
            continue
        any_fail = True
        print(
            f"  [{r.article_id}] {r.title[:60]}\n"
            f"    expected: {r.expected}\n"
            f"    actual  : event_type={r.actual.get('event_type')} "
            f"impact={r.actual.get('impact_level')} "
            f"sentiment={r.actual.get('sentiment')} score={r.actual.get('sentiment_score'):.2f} "
            f"keywords={r.actual.get('keywords')} sectors={r.actual.get('sector_tags')}\n"
            f"    fails   : {fails}"
        )
    if not any_fail:
        print("  (모두 통과)")


async def _run_one(extractor: LiteLLMEventExtractor, item: dict) -> CaseResult:
    article = _to_article(item)
    try:
        event = await extractor.extract(article)
    except Exception as exc:
        return CaseResult(
            article_id=item["article_id"],
            title=item["title"],
            expected=item.get("expected", {}),
            actual={"event_type": "__extract_failed__", "error": str(exc)},
            hits={},
        )
    hits = _grade(item.get("expected", {}), event)
    actual = event.model_dump(mode="json")
    return CaseResult(
        article_id=item["article_id"],
        title=item["title"],
        expected=item.get("expected", {}),
        actual=actual,
        hits=hits,
    )


async def _amain(args: argparse.Namespace) -> int:
    fixtures = _load_fixtures(Path(args.fixture))
    if args.limit:
        fixtures = fixtures[: args.limit]

    extractor = LiteLLMEventExtractor(
        model=args.model,
        api_base=args.api_base,
        temperature=args.temperature,
    )

    sem = asyncio.Semaphore(args.concurrency)

    async def _bounded(item: dict) -> CaseResult:
        async with sem:
            return await _run_one(extractor, item)

    results = await asyncio.gather(*(_bounded(f) for f in fixtures))

    agg = _aggregate(list(results))
    _print_report(list(results), agg)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": args.model,
            "api_base": args.api_base,
            "fixture": str(args.fixture),
            "generated_at": datetime.now().isoformat(),
            "aggregate": agg,
            "cases": [
                {
                    "article_id": r.article_id,
                    "title": r.title,
                    "expected": r.expected,
                    "actual": r.actual,
                    "hits": r.hits,
                }
                for r in results
            ],
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nsaved: {out_path}")

    # CI 친화적 exit code: event_type acc < threshold → 비정상
    threshold = args.fail_under
    if threshold is not None and agg["event_type_acc"] < threshold:
        print(f"\nFAIL: event_type_acc {agg['event_type_acc']:.1%} < threshold {threshold:.1%}")
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="News Event Extractor evaluation harness")
    p.add_argument(
        "--fixture",
        default="tests/fixtures/news_eval/baseline.json",
        help="path to fixture JSON",
    )
    p.add_argument(
        "--model",
        default=os.environ.get(
            "NEWS_EVAL_MODEL",
            "hosted_vllm/jart25/Qwen3-30B-A3B-Instruct-2507-Autoround-Int-4bit-gptq",
        ),
        help="LiteLLM model id (default from NEWS_EVAL_MODEL env)",
    )
    p.add_argument(
        "--api-base",
        default=os.environ.get("NEWS_EVAL_API_BASE", "http://127.0.0.1:8001/v1"),
        help="OpenAI-compatible base URL (default from NEWS_EVAL_API_BASE env)",
    )
    p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--concurrency", type=int, default=4, help="parallel extract 수")
    p.add_argument("--limit", type=int, default=None, help="처음 N 건만")
    p.add_argument(
        "--json-out",
        default=None,
        help="aggregate + per-case JSON 저장 경로",
    )
    p.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="event_type_acc 이 이 값 미만이면 exit 1 (CI regression guard)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
