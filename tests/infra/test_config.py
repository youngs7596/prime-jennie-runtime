"""``validate_kis_gateway_url`` 검증."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.infra.config import (
    KISConfig,
    validate_kis_gateway_url,
)


def _cfg(gateway_url: str = "") -> KISConfig:
    # gateway_url 기본값을 빈 문자열로 강제하려면 명시 인자 필요.
    return KISConfig(gateway_url=gateway_url)


def test_empty_env_and_empty_cfg_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("KIS_GATEWAY_URL", raising=False)
    resolved = validate_kis_gateway_url(_cfg(""), strict=False)
    assert resolved == "http://kis-gateway:8080"


def test_strict_mode_raises_when_empty(monkeypatch):
    monkeypatch.delenv("KIS_GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="KIS_GATEWAY_URL"):
        validate_kis_gateway_url(_cfg(""), strict=True)


def test_env_wins_over_cfg(monkeypatch):
    monkeypatch.setenv("KIS_GATEWAY_URL", "http://from-env:9000/")
    resolved = validate_kis_gateway_url(_cfg("http://from-cfg:8080"))
    assert resolved == "http://from-env:9000"


def test_cfg_used_when_env_missing(monkeypatch):
    monkeypatch.delenv("KIS_GATEWAY_URL", raising=False)
    resolved = validate_kis_gateway_url(_cfg("http://from-cfg:8080"))
    assert resolved == "http://from-cfg:8080"


def test_localhost_strict_raises(monkeypatch):
    monkeypatch.setenv("KIS_GATEWAY_URL", "http://localhost:8080")
    with pytest.raises(RuntimeError, match="localhost"):
        validate_kis_gateway_url(_cfg(""), strict=True)


def test_localhost_non_strict_warns_but_returns(monkeypatch, caplog):
    monkeypatch.setenv("KIS_GATEWAY_URL", "http://127.0.0.1:8080")
    with caplog.at_level("WARNING", logger="prime_jennie_runtime.infra.config"):
        resolved = validate_kis_gateway_url(_cfg(""), strict=False)
    assert resolved == "http://127.0.0.1:8080"
    assert any("localhost" in r.message for r in caplog.records)


def test_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("KIS_GATEWAY_URL", "http://gw:8080/")
    assert validate_kis_gateway_url(_cfg(""), strict=False) == "http://gw:8080"


def test_default_kis_config_gateway_url_is_docker_service(monkeypatch):
    """KISConfig 의 default gateway_url 이 docker 서비스 이름이어야 함 (회귀 방지)."""
    # env 와 .env 둘 다 끄고 디폴트 확인
    monkeypatch.delenv("KIS_GATEWAY_URL", raising=False)
    cfg = KISConfig()
    assert cfg.gateway_url == "http://kis-gateway:8080"
