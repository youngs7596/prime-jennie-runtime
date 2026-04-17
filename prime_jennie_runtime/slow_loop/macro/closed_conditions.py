"""결정론적 closed 조건 검증.

MACRO_GATE_SPEC §2.3, §5.2. LLM 판단과 **독립적으로** 코드가 closed 조건을 재검증.
LLM이 open을 내놓아도 트리거가 있으면 auto_override로 closed 강제.

트리거:
- fx_shock: KRW/USD 일일 ±3% 이상 변동
- high_volatility: KOSPI 20d vol >= 35%
- sector_contagion: 주요 섹터 3개 이상 동시 -5% 이상 하락

지정학/유동성 트리거는 LLM에 위임 (코드로 판단 불가).
"""

from __future__ import annotations

from .schemas import MarketSnapshot

FX_SHOCK_THRESHOLD = 0.03  # KRW/USD ±3%
HIGH_VOL_THRESHOLD = 0.35  # KOSPI 20d vol 35%
SECTOR_DROP_THRESHOLD = -0.05  # 섹터 -5%
SECTOR_CONTAGION_COUNT = 3  # 3개 이상


def check_closed_conditions(snap: MarketSnapshot) -> list[str]:
    """트리거된 closed 조건 리스트를 반환. 비어있으면 어떤 조건도 충족하지 않음."""
    triggers: list[str] = []

    if snap.usd_krw_change_abs >= FX_SHOCK_THRESHOLD:
        triggers.append("fx_shock")

    if snap.kospi_20d_vol >= HIGH_VOL_THRESHOLD:
        triggers.append("high_volatility")

    major_drops = sum(1 for s in snap.major_sector_drops if s.change_pct <= SECTOR_DROP_THRESHOLD)
    if major_drops >= SECTOR_CONTAGION_COUNT:
        triggers.append("sector_contagion")

    return triggers
