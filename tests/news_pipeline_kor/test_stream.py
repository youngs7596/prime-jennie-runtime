"""Stream payload 직렬화 round-trip."""

from __future__ import annotations

from prime_jennie_runtime.news_pipeline_kor import make_dummy_article
from prime_jennie_runtime.news_pipeline_kor.stream import (
    deserialize_article,
    serialize_article,
)


def test_serialize_deserialize_roundtrip_str_keys():
    original = make_dummy_article("005930", title="삼성전자 신고가")
    payload = serialize_article(original)
    assert "article_json" in payload
    restored = deserialize_article(payload)
    assert restored.article_id == original.article_id
    assert restored.ticker == original.ticker
    assert restored.title == original.title


def test_deserialize_accepts_bytes_keys_and_values():
    original = make_dummy_article("000660", title="bytes path")
    payload = serialize_article(original)
    # Redis Streams w/ decode_responses=False 시 나오는 형태 모사
    bytes_payload = {k.encode(): v.encode() for k, v in payload.items()}
    restored = deserialize_article(bytes_payload)
    assert restored.ticker == "000660"
    assert restored.title == "bytes path"
