"""공유 테스트 픽스처."""

from __future__ import annotations

import fakeredis.aioredis
import pytest


@pytest.fixture
def fake_redis():
    """fakeredis async 클라이언트."""
    return fakeredis.aioredis.FakeRedis(decode_responses=False)
