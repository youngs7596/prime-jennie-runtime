"""KIS 토큰 파일 I/O 전담.

v2 `kis_api._load_cached_token`/`_save_cached_token` 를 모듈 함수로 분리.
디스크 캐시만 담당 — 발급/만료 판정은 KISApi 내부에서 수행.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenRecord:
    """디스크 캐시에 저장되는 KIS 액세스 토큰."""

    access_token: str
    expires_at: float  # epoch seconds


def load_token(path: str | Path) -> TokenRecord | None:
    """파일에서 토큰 로드. 존재하지 않거나 파싱 실패 시 None."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return TokenRecord(
            access_token=raw["access_token"],
            expires_at=float(raw["expires_at"]),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        logger.warning("Cached token parse failed at %s: %s", p, e)
        return None


def save_token(path: str | Path, record: TokenRecord) -> None:
    """토큰을 파일에 저장. 부모 디렉토리가 없으면 생성."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "access_token": record.access_token,
                    "expires_at": record.expires_at,
                }
            )
        )
    except OSError as e:
        logger.warning("Failed to cache KIS token at %s: %s", p, e)
