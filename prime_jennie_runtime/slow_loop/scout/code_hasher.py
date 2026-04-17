"""Scout 생성 코드 해시 유틸.

provenance.scout_code_hash에 쓰이는 sha256 포맷. 재현성 검증(S13)에 사용.
"""

from __future__ import annotations

import hashlib


def compute_code_hash(code: str) -> str:
    """Scout 코드의 sha256 해시를 `sha256:<64hex>` 형식으로 반환.

    공백/줄바꿈은 정규화하지 않는다 — 같은 소스가 들어오면 같은 해시.
    정규화가 필요하다면 호출자가 사전 처리.
    """
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
