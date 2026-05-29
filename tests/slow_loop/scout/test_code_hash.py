"""Scout 코드 해시 재현성 테스트 (S13)."""

from __future__ import annotations

from prime_jennie_runtime.slow_loop.scout.deterministic_scout import compute_code_hash


def test_same_code_same_hash():
    code = "def screen(market_data, context):\n    return []\n"
    h1 = compute_code_hash(code)
    h2 = compute_code_hash(code)
    assert h1 == h2


def test_hash_format():
    h = compute_code_hash("x = 1")
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_different_code_different_hash():
    h1 = compute_code_hash("x = 1")
    h2 = compute_code_hash("x = 2")
    assert h1 != h2


def test_unicode_code():
    code = "def screen(md, ctx):\n    # 한글 주석\n    return []\n"
    h = compute_code_hash(code)
    assert h.startswith("sha256:")
