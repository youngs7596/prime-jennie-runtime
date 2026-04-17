"""kure-v1 embedder — vLLM OpenAI-호환 `/v1/embeddings` 엔드포인트 호출.

v2 ``archiver.py`` 는 LangChain ``OpenAIEmbeddings`` 로 래핑했지만 v3 는 직접
``httpx.AsyncClient`` 로 OpenAI 포맷 요청. 가벼움 + 주입 테스트 용이.

응답 shape (OpenAI): ``{"data": [{"embedding": [...], ...}], "model": "kure-v1"}``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..models import NewsArticle

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "kure-v1"
DEFAULT_URL = "http://localhost:8002/v1"
DEFAULT_DIMENSION = 1024  # kure-v1 실 차원


@dataclass
class KureEmbedder:
    """kure-v1 (vLLM) 임베딩 호출. ``Embedder`` Protocol 구현.

    Article 의 ``"[ticker] title"`` 를 임베딩 (v2 archiver 와 동일한 content 조합).
    """

    api_base: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    dimension: int = DEFAULT_DIMENSION
    timeout_s: float = 15.0
    api_key: str = "not-needed"  # vLLM 로컬이면 무의미
    client: httpx.AsyncClient | None = None

    async def embed(self, article: NewsArticle) -> list[float]:
        text = self._build_text(article)
        client, owns = self._ensure_client()
        try:
            resp = await client.post(
                f"{self.api_base.rstrip('/')}/embeddings",
                json={"input": text, "model": self.model},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as e:
            logger.warning("kure embed failed: %s", e)
            return self._zero_vector()
        finally:
            if owns:
                await client.aclose()

        if resp.status_code != 200:
            logger.warning("kure embed status=%d body=%s", resp.status_code, resp.text[:200])
            return self._zero_vector()

        try:
            data = resp.json()
            vec = data["data"][0]["embedding"]
            if not isinstance(vec, list) or not vec:
                return self._zero_vector()
            return [float(x) for x in vec]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            logger.warning("kure embed parse failed: %s", e)
            return self._zero_vector()

    def _build_text(self, article: NewsArticle) -> str:
        body = article.body.strip()
        if body:
            return f"[{article.ticker}] {article.title}\n{body[:500]}"
        return f"[{article.ticker}] {article.title}"

    def _ensure_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.client is not None:
            return self.client, False
        return httpx.AsyncClient(timeout=self.timeout_s), True

    def _zero_vector(self) -> list[float]:
        return [0.0] * self.dimension


__all__ = ["DEFAULT_MODEL", "DEFAULT_URL", "KureEmbedder"]
