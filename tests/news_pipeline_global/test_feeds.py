"""feeds.load_feeds — 기본값 + env override."""

from __future__ import annotations

import json

import pytest

from prime_jennie_runtime.news_pipeline_global.feeds import DEFAULT_FEEDS, load_feeds


def test_default_feeds_non_empty():
    assert len(DEFAULT_FEEDS) >= 3
    sources = {f.source for f in DEFAULT_FEEDS}
    assert {"wsj", "bloomberg"} <= sources


def test_load_feeds_env_override(monkeypatch):
    payload = [
        {"source": "custom", "feed_name": "my-feed", "url": "https://x.example.com/rss"},
    ]
    monkeypatch.setenv("GLOBAL_NEWS_FEEDS_JSON", json.dumps(payload))
    feeds = load_feeds()
    assert len(feeds) == 1
    assert feeds[0].source == "custom"
    assert feeds[0].url == "https://x.example.com/rss"


def test_load_feeds_invalid_json_raises(monkeypatch):
    monkeypatch.setenv("GLOBAL_NEWS_FEEDS_JSON", "not-json")
    with pytest.raises((ValueError, KeyError, TypeError)):
        load_feeds()
