"""Qdrant VectorStore 어댑터 — v3 ``VectorStore`` Protocol 구현.

D1 결정: v3 전용 컬렉션 ``v3_news_sentiments`` (v2 공존기 충돌 없음).

운영:
- vector: kure-v1 임베딩 (1024d) — cosine 거리
- payload: ticker, article_id, model, analyzed_at
- point_id: article_id 의 MD5 hex (Qdrant 는 UUID/uint 만 허용 → hex 변환 후 uint)

테스트:
- ``QdrantClient(location=":memory:")`` 지원 → 실 컬렉션 E2E in-memory.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from ..models import NewsEmbedding

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "v3_news_sentiments"


@dataclass
class QdrantVectorStore:
    """qdrant-client 래퍼. ``client`` 주입 또는 URL 로 연결.

    생성 시 컬렉션 없으면 자동 생성 (``dimension`` + cosine).
    """

    client: Any  # qdrant_client.QdrantClient
    collection_name: str = DEFAULT_COLLECTION
    dimension: int = 1024
    _initialized: bool = field(default=False, init=False)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        collection_name: str = DEFAULT_COLLECTION,
        dimension: int = 1024,
    ) -> QdrantVectorStore:
        from qdrant_client import QdrantClient

        return cls(
            client=QdrantClient(url=url), collection_name=collection_name, dimension=dimension
        )

    @classmethod
    def in_memory(
        cls,
        *,
        collection_name: str = DEFAULT_COLLECTION,
        dimension: int = 1024,
    ) -> QdrantVectorStore:
        """단위/통합 테스트용 (``:memory:`` 클라이언트)."""
        from qdrant_client import QdrantClient

        return cls(
            client=QdrantClient(location=":memory:"),
            collection_name=collection_name,
            dimension=dimension,
        )

    def _ensure_collection(self) -> None:
        if self._initialized:
            return
        from qdrant_client.models import Distance, VectorParams

        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection_name not in existing:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.dimension, distance=Distance.COSINE),
            )
            logger.info(
                "qdrant created collection %s (dim=%d)", self.collection_name, self.dimension
            )
        self._initialized = True

    async def upsert(self, embedding: NewsEmbedding) -> None:
        self._ensure_collection()
        from qdrant_client.models import PointStruct

        point_id = _article_id_to_point_id(embedding.article_id)
        point = PointStruct(
            id=point_id,
            vector=list(embedding.vector),
            payload={
                "article_id": embedding.article_id,
                "ticker": embedding.ticker,
                "model": embedding.model,
                "embedded_at": embedding.embedded_at.isoformat(),
            },
        )
        self.client.upsert(collection_name=self.collection_name, points=[point])

    async def top_k(
        self,
        query_vector: list[float],
        *,
        ticker: str | None,
        k: int,
    ) -> list[tuple[str, float]]:
        self._ensure_collection()
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        query_filter: Filter | None = None
        if ticker is not None:
            query_filter = Filter(
                must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))]
            )

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=k,
            with_payload=True,
        ).points

        out: list[tuple[str, float]] = []
        for point in results:
            payload = point.payload or {}
            aid = str(payload.get("article_id") or point.id)
            out.append((aid, float(point.score)))
        return out


def _article_id_to_point_id(article_id: str) -> int:
    """Qdrant point_id 는 uint64 또는 UUID. article_id 를 MD5 hash → uint64 로."""
    digest = hashlib.md5(article_id.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=False)


__all__ = ["DEFAULT_COLLECTION", "QdrantVectorStore"]
