"""sheet_id 생성 유틸.

POSITION_SHEET_SPEC §2.1: `ps_YYYYMMDD_TTTTTT_HHHH` (TTTTTT=6digit ticker, HHHH=4hex).
같은 (ticker, generated_at)에서 같은 sheet_id가 나와야 멱등성 확보.
"""

from __future__ import annotations

import hashlib
from datetime import datetime


def generate_sheet_id(ticker: str, generated_at: datetime, salt: str = "") -> str:
    """(ticker, generated_at, salt)를 sha256한 뒤 앞 4hex를 꼬리로 붙인다.

    - salt: 같은 ticker + 같은 초에 여러 시트를 발행해야 하는 드문 경우 (예: 수동 오버라이드
      재시도)에 호출자가 구별용으로 넣는다. 기본 공백.
    """
    if len(ticker) != 6 or not ticker.isdigit():
        raise ValueError(f"ticker must be 6 digits, got: {ticker!r}")

    date_str = generated_at.strftime("%Y%m%d")
    seed = f"{ticker}|{generated_at.isoformat()}|{salt}"
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:4]
    return f"ps_{date_str}_{ticker}_{suffix}"
