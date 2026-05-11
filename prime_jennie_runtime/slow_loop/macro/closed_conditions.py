"""결정론적 closed 조건 검증.

MACRO_GATE_SPEC §2.3, §5.2. LLM 판단과 **독립적으로** 코드가 closed 조건을 재검증.
LLM이 open을 내놓아도 트리거가 있으면 auto_override로 closed 강제.

트리거:
- fx_shock: KRW/USD 일일 ±3% 이상 변동
- high_volatility: KOSPI 20d vol >= 35%
- sector_contagion: 주요 섹터 3개 이상 동시 -5% 이상 하락
- geopolitical: 24h high-impact geopolitical 이벤트 >= 3건 (한반도/중동/대만 등)
- liquidity_crunch: 거래대금 비율 <= 0.5 AND 스프레드 비율 >= 2.0
   (오탐 방지 위해 두 신호 AND 결합 — 단일 지표는 노이즈 가능성 큼)

geopolitical / liquidity 는 강하게 (보수적) 셋업 — 오탐 시 매매 차단 비용
크므로 임계값을 의도적으로 높게.
"""

from __future__ import annotations

from .schemas import MarketSnapshot

FX_SHOCK_THRESHOLD = 0.03  # KRW/USD ±3%
HIGH_VOL_THRESHOLD = 0.35  # KOSPI 20d vol 35%
SECTOR_DROP_THRESHOLD = -0.05  # 섹터 -5%
SECTOR_CONTAGION_COUNT = 3  # 3개 이상

# 지정학 — high impact 이벤트 3건 이상 (보수적). 1~2건은 LLM 판단에 위임.
GEOPOLITICAL_HIGH_IMPACT_THRESHOLD = 3

# 유동성 — 두 조건 동시 충족 (AND) 일 때만 트리거.
# 거래대금 50% 이하 (즉 ratio <= 0.5) AND 스프레드 2배 이상 (ratio >= 2.0).
LIQUIDITY_TURNOVER_RATIO_THRESHOLD = 0.5
LIQUIDITY_SPREAD_RATIO_THRESHOLD = 2.0


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

    # 지정학 결정론 트리거 — 24h 누적 high-impact geopolitical 이벤트 다수.
    # 단일 사건은 LLM 판단에 맡기되 3건 이상 누적이면 코드가 강제 closed.
    if snap.high_impact_geopolitical_count >= GEOPOLITICAL_HIGH_IMPACT_THRESHOLD:
        triggers.append("geopolitical")

    # 유동성 경색 — 거래대금 급감 AND 스프레드 급확대 동시 충족 (AND 결합).
    # 두 지표 중 하나가 None 이면 평가 skip (fail-open).
    if (
        snap.kospi_turnover_ratio is not None
        and snap.kospi_spread_ratio is not None
        and snap.kospi_turnover_ratio <= LIQUIDITY_TURNOVER_RATIO_THRESHOLD
        and snap.kospi_spread_ratio >= LIQUIDITY_SPREAD_RATIO_THRESHOLD
    ):
        triggers.append("liquidity_crunch")

    return triggers
