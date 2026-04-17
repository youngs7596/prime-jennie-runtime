"""Phase A 운영전환 smoke — MS-01에서 실행.

각 단계:
  1. Qdrant 컬렉션 확인/생성 (v3_news_sentiments)
  2. kure-v1 임베더로 샘플 기사 벡터화
  3. Qdrant upsert + top_k 검색 (자기 자신이 top1 인지 확인)
  4. EXAONE sentiment 분석 (vLLM 경유)
  5. Telegram sendMessage (요약 리포트 발신)

각 단계 실패해도 다음 단계 진행. 마지막에 PASS/FAIL 테이블 출력.

실행:
    uv run python -m scripts.phase_a_ops_smoke

환경변수 (모두 .env 로드):
    VLLM_LLM_URL / VLLM_EMBED_URL / VLLM_LLM_MODEL / VLLM_EMBED_MODEL
    QDRANT_URL / QDRANT_COLLECTION_V3
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("phase_a_smoke")


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


async def step_qdrant_init(url: str, collection: str, dimension: int) -> StepResult:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=url)
    existing = {c.name for c in client.get_collections().collections}
    created = False
    if collection not in existing:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
        )
        created = True
    info = client.get_collection(collection_name=collection)
    detail = (
        f"collection={collection} created={created} "
        f"vectors={info.points_count} dim={dimension}"
    )
    return StepResult(name="qdrant_init", ok=True, detail=detail)


async def step_kure_embed_roundtrip(
    *,
    vllm_embed_url: str,
    vllm_embed_model: str,
    qdrant_url: str,
    collection: str,
) -> tuple[StepResult, int]:
    from qdrant_client import QdrantClient

    from prime_jennie_runtime.news_pipeline_kor.adapters.kure_embedder import KureEmbedder
    from prime_jennie_runtime.news_pipeline_kor.adapters.qdrant_store import QdrantVectorStore
    from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle, NewsEmbedding

    embedder = KureEmbedder(api_base=vllm_embed_url, model=vllm_embed_model)
    article = NewsArticle(
        article_id="smoke-phase-a-001",
        ticker="005930",
        title="삼성전자, 1분기 영업이익 호실적 발표",
        body="삼성전자가 1분기 영업이익이 전년 대비 크게 증가했다고 발표했다.",
        published_at=datetime.now(tz=UTC),
        source_url="https://example.com/smoke/001",
        source_name="smoke",
    )
    vec = await embedder.embed(article)
    if not vec or len(vec) == 0 or all(x == 0.0 for x in vec):
        return (
            StepResult(
                name="kure_embed",
                ok=False,
                detail=f"zero/empty vector returned (len={len(vec)})",
            ),
            len(vec),
        )
    dim = len(vec)

    store = QdrantVectorStore(
        client=QdrantClient(url=qdrant_url), collection_name=collection, dimension=dim
    )
    emb = NewsEmbedding(
        article_id=article.article_id,
        ticker=article.ticker,
        vector=vec,
        embedded_at=datetime.now(tz=UTC),
        model=vllm_embed_model,
    )
    await store.upsert(emb)
    top = await store.top_k(query_vector=vec, ticker=article.ticker, k=1)
    if not top:
        return (
            StepResult(name="kure_embed", ok=False, detail=f"upsert ok dim={dim} but top_k empty"),
            dim,
        )
    aid, score = top[0]
    ok = aid == article.article_id and score > 0.99
    return (
        StepResult(
            name="kure_embed",
            ok=ok,
            detail=f"dim={dim} top1={aid} score={score:.4f}",
        ),
        dim,
    )


async def step_exaone_sentiment(
    *,
    vllm_llm_url: str,
    vllm_llm_model: str,
) -> StepResult:
    from prime_jennie_runtime.news_pipeline_kor.adapters.exaone_sentiment import (
        LiteLLMSentimentAnalyzer,
    )
    from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle

    analyzer = LiteLLMSentimentAnalyzer(
        model=f"openai/{vllm_llm_model}",
        api_base=vllm_llm_url,
        extra_kwargs={"api_key": "not-needed"},
    )
    body = (
        "삼성전자는 반도체 수요 회복과 환율 효과로 "
        "1분기 영업이익이 사상 최대를 기록했다고 발표했다."
    )
    article = NewsArticle(
        article_id="smoke-sent-001",
        ticker="005930",
        title="삼성전자, 1분기 영업이익 사상 최대",
        body=body,
        published_at=datetime.now(tz=UTC),
        source_url="https://example.com/smoke/sent/001",
        source_name="smoke",
    )
    result = await analyzer.analyze(article)
    ok = result.score > 0.2  # 호실적이므로 positive 기대
    return StepResult(
        name="exaone_sentiment",
        ok=ok,
        detail=f"score={result.score:+.3f} label={result.label} model={result.model}",
    )


async def step_telegram_send(summary: str) -> StepResult:
    from prime_jennie_runtime.infra.config import TelegramConfig
    from prime_jennie_runtime.telegram_bot.bot import TelegramBot

    cfg = TelegramConfig()
    if not cfg.bot_token or not cfg.chat_id:
        return StepResult(
            name="telegram_send",
            ok=False,
            detail="bot_token/chat_id not configured",
        )
    bot = TelegramBot(cfg)
    try:
        sent = await bot.send_message(summary)
    finally:
        await bot.close()
    return StepResult(
        name="telegram_send",
        ok=bool(sent),
        detail=f"chat_id={cfg.chat_id} sent={sent}",
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-telegram", action="store_true")
    parser.add_argument("--dimension", type=int, default=1024, help="kure-v1 기본 차원")
    args = parser.parse_args()

    # .env 로드 (pydantic-settings 가 자동으로 읽지만, 하단 헬퍼용으로 python-dotenv 도 시도)
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    qdrant_url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
    collection = os.environ.get("QDRANT_COLLECTION_V3", "v3_news_sentiments")
    vllm_embed_url = os.environ.get("VLLM_EMBED_URL", "http://127.0.0.1:8002/v1")
    vllm_llm_url = os.environ.get("VLLM_LLM_URL", "http://127.0.0.1:8001/v1")
    vllm_embed_model = os.environ.get("VLLM_EMBED_MODEL", "nlpai-lab/KURE-v1")
    vllm_llm_model = os.environ.get("VLLM_LLM_MODEL", "LGAI-EXAONE/EXAONE-4.0-32B-AWQ")

    logging.basicConfig(
        level=os.environ.get("APP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    results: list[StepResult] = []

    # Step 1: Qdrant
    try:
        results.append(await step_qdrant_init(qdrant_url, collection, args.dimension))
    except Exception as e:
        traceback.print_exc()
        results.append(StepResult(name="qdrant_init", ok=False, detail=f"{type(e).__name__}: {e}"))

    # Step 2+3: kure embed + roundtrip
    dim = args.dimension
    try:
        r, dim = await step_kure_embed_roundtrip(
            vllm_embed_url=vllm_embed_url,
            vllm_embed_model=vllm_embed_model,
            qdrant_url=qdrant_url,
            collection=collection,
        )
        results.append(r)
    except Exception as e:
        traceback.print_exc()
        results.append(StepResult(name="kure_embed", ok=False, detail=f"{type(e).__name__}: {e}"))

    # Step 4: EXAONE
    try:
        results.append(
            await step_exaone_sentiment(vllm_llm_url=vllm_llm_url, vllm_llm_model=vllm_llm_model)
        )
    except Exception as e:
        traceback.print_exc()
        results.append(
            StepResult(name="exaone_sentiment", ok=False, detail=f"{type(e).__name__}: {e}")
        )

    # Step 5: Telegram
    if args.skip_telegram:
        results.append(StepResult(name="telegram_send", ok=True, detail="SKIPPED"))
    else:
        summary_lines = [f"[Phase A smoke] {datetime.now(tz=UTC).isoformat(timespec='seconds')}"]
        for r in results:
            flag = "PASS" if r.ok else "FAIL"
            summary_lines.append(f"{flag} {r.name}: {r.detail}")
        summary = "\n".join(summary_lines)
        try:
            results.append(await step_telegram_send(summary))
        except Exception as e:
            traceback.print_exc()
            results.append(
                StepResult(name="telegram_send", ok=False, detail=f"{type(e).__name__}: {e}")
            )

    print("\n=== Phase A smoke report ===")
    for r in results:
        flag = "PASS" if r.ok else "FAIL"
        print(f"  [{flag}] {r.name:<20s} | {r.detail}")
    fail = [r for r in results if not r.ok]
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
