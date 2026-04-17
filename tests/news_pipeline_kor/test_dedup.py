"""dedup 모듈 — v2 fingerprint 호환 + 3일 윈도우 동작."""

from __future__ import annotations

from datetime import datetime, timedelta

from prime_jennie_runtime.news_pipeline_kor.dedup import (
    DEDUP_WINDOW_DAYS,
    InMemoryDeduplicator,
    article_fingerprint,
)

# ---- fingerprint ----


def test_fingerprint_v2_compatible_lower_strip():
    """v2의 hashlib.md5(text.strip().lower()).hexdigest()[:12]와 동일."""
    a = article_fingerprint("https://example.com/news/1")
    b = article_fingerprint("  HTTPS://EXAMPLE.com/news/1  ")
    assert a == b
    assert len(a) == 12


def test_fingerprint_distinguishes_distinct_urls():
    a = article_fingerprint("https://example.com/news/1")
    b = article_fingerprint("https://example.com/news/2")
    assert a != b


# ---- InMemoryDeduplicator ----


def test_first_call_is_new_second_is_dup():
    d = InMemoryDeduplicator()
    assert d.is_new("https://x.com/a") is True
    assert d.is_new("https://x.com/a") is False


def test_is_new_independent_per_url():
    d = InMemoryDeduplicator()
    assert d.is_new("https://x.com/a") is True
    assert d.is_new("https://x.com/b") is True
    assert d.size() == 2


def test_dedup_window_3_days():
    """오늘에서 봤을 때 2일 전까지는 중복으로 인식. 4일 전은 신규로 인식."""
    d = InMemoryDeduplicator(window_days=DEDUP_WINDOW_DAYS)
    t_today = datetime(2026, 4, 17, 12, 0, 0)
    t_2_days_ago = t_today - timedelta(days=2)
    t_3_days_ago = t_today - timedelta(days=3)

    # 2일 전에 등록
    assert d.is_new("u1", now=t_2_days_ago) is True
    # 오늘 다시 보면 dup (2일 ≤ window=3)
    assert d.is_new("u1", now=t_today) is False

    # 3일 전 등록한 건 윈도우 밖 (range(3) → 0,1,2 일까지 검사)
    assert d.is_new("u2", now=t_3_days_ago) is True
    assert d.is_new("u2", now=t_today) is True  # 신규로 처리됨


def test_dedup_url_normalization_dedups_case_diff():
    d = InMemoryDeduplicator()
    assert d.is_new("https://x.com/Article") is True
    assert d.is_new("HTTPS://X.COM/Article") is False
