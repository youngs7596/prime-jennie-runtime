"""네이버 시장 전체 투자자 수급 JSON 응답 fixture — 여러 테스트가 같이 쓴다.

2026-10 출처 교체 뒤의 실제 구조(`stock.naver.com/api/domestic/market/trend/daily`)를
재현한다. 거래일마다 투자자 코드별 순매수를 원 단위 문자열로 준다.
"""

from __future__ import annotations

INVESTOR_TREND_URL_RE = r"https://stock\.naver\.com/api/domestic/market/trend/daily.*"

_WON_PER_EOK = 100_000_000

# 2026-06-23 실측치(억원). 투신(사모)는 투신 3000 과 사모 3100 으로 나눠 넣는다.
DEFAULT_EOK: dict[str, float] = {
    "individual": 85910,
    "foreign": -42047,
    "financial_inv": -20908,
    "insurance": -1018,
    "trust": -19662,
    "bank": -165,
    "etc_finance": -69,
    "pension": -2937,
    "etc_corp": 898,
}


def trend_row(bizdate: str, **eok: float) -> dict:
    """한 거래일 줄. 키워드로 억원 값을 바꿔 넣을 수 있다."""
    v = {**DEFAULT_EOK, **eok}

    def won(x: float) -> str:
        return str(int(x * _WON_PER_EOK))

    codes = [
        ("8000", v["individual"]),
        ("9000", v["foreign"] - 1),  # 외국인 + 기타외국인 = foreign
        ("9001", 1),
        ("1000", v["financial_inv"]),
        ("2000", v["insurance"]),
        ("3000", v["trust"] - 100),  # 투신 + 사모 = trust
        ("3100", 100),
        ("4000", v["bank"]),
        ("5000", v["etc_finance"]),
        ("6000", v["pension"]),
        ("7000", 0),
        ("7100", v["etc_corp"]),
    ]
    return {
        "bizdate": bizdate,
        "time": "",
        "netAmounts": [{"investorGubun": c, "diffValue": won(x)} for c, x in codes],
    }


def trend_json(*rows: dict) -> dict:
    return {"content": list(rows), "first": True, "last": False}
