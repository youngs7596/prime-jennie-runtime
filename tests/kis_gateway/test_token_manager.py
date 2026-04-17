"""token_manager 단위 테스트."""

from __future__ import annotations

from pathlib import Path

from prime_jennie_runtime.kis_gateway.token_manager import (
    TokenRecord,
    load_token,
    save_token,
)


def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "token.json"
    record = TokenRecord(access_token="abc123", expires_at=123456.789)
    save_token(path, record)

    loaded = load_token(path)
    assert loaded is not None
    assert loaded.access_token == "abc123"
    assert loaded.expires_at == 123456.789


def test_load_missing_file(tmp_path: Path):
    assert load_token(tmp_path / "missing.json") is None


def test_load_malformed_json(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    assert load_token(path) is None


def test_load_missing_key(tmp_path: Path):
    path = tmp_path / "partial.json"
    path.write_text('{"access_token": "x"}')
    assert load_token(path) is None


def test_save_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "nested" / "deep" / "token.json"
    save_token(path, TokenRecord(access_token="t", expires_at=1.0))
    assert path.exists()
