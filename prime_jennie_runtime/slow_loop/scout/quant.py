"""결정론 선정 — Phase 3 Quant Scorer v2 (잠재력 기반 7팩터 스코어링).

v2 `prime_jennie/services/scout/quant.py` 의 **faithful 포팅** (2026-05-22, Phase 1 a).
팩터 수식·임계값·버킷은 v2 원본을 그대로 옮긴다. v3 적응:
  - 입력 타입(`EnrichedCandidate`)은 `slow_loop/scout/enrichment.py` 로컬 포팅본.
  - `QuantScore` 를 이 모듈에 로컬 정의 (v2 domain/scoring.py). v2 의 미사용 필드
    matched_conditions / condition_win_rate / condition_confidence 는 v2 teardown
    §3 에서 dead 로 확인 — 포팅 제외.
  - v2 `score_candidate` 는 `market_regime: MarketRegime` 를 받아 즉시
    `is_bull` bool 로 환산했다. v3 macro 는 5단계 regime 이 없고 gate+size_multiplier
    뿐이므로, 이 포팅은 `is_bull: bool` 을 직접 받는다 (호출부 deterministic_scout
    가 size_multiplier 로부터 환산). 내부 모멘텀 로직은 v2 와 동일.
  - sector_group 은 str (v2 SectorGroup enum — quant 는 dict 키로만 사용).

7개 서브팩터 (가중치 합 110, total 은 100 캡):
  - 모멘텀 0-20 / 품질 0-20 / 가치 0-20 / 기술 0-10 / 뉴스 0-10 / 수급 0-20 / 섹터모멘텀 0-10
"""

from __future__ import annotations

import logging
from typing import Self

from pydantic import BaseModel, model_validator

from .enrichment import DailyPrice, EnrichedCandidate

logger = logging.getLogger(__name__)


# =====================================================================
# QuantScore — v2 domain/scoring.py QuantScore 포팅 (dead 필드 3종 제외)
# =====================================================================


class QuantScore(BaseModel):
    """Quant Scorer v2 출력 — 7개 서브팩터 합산 (100pt 캡)."""

    stock_code: str
    stock_name: str
    total_score: float
    momentum_score: float = 0.0  # 0-20
    quality_score: float = 0.0  # 0-20
    value_score: float = 0.0  # 0-20
    technical_score: float = 0.0  # 0-10
    news_score: float = 0.0  # 0-10
    supply_demand_score: float = 0.0  # 0-20
    sector_momentum_score: float = 0.0  # 0-10
    is_valid: bool = True
    invalid_reason: str | None = None

    @model_validator(mode="after")
    def check_total_vs_subscores(self) -> Self:
        raw_sum = (
            self.momentum_score
            + self.quality_score
            + self.value_score
            + self.technical_score
            + self.news_score
            + self.supply_demand_score
            + self.sector_momentum_score
        )
        expected = max(0.0, min(100.0, raw_sum))
        if abs(self.total_score - expected) > 1.5:
            raise ValueError(
                f"total_score({self.total_score:.1f}) != capped subscores({expected:.1f}), "
                f"diff={abs(self.total_score - expected):.1f}"
            )
        return self


# ─── v2 가중치 ───────────────────────────────────────────────────
# NOTE (faithful port): v2 의 가중치 합은 20+20+20+10+10+20+10 = 110 이지만
# total 은 min(100, ...) 으로 캡된다. v2 teardown §3 이 지적한 110/100 미스매치를
# 그대로 이식한다 — 가중치 조정은 선정 거동 변경이라 순수 포팅과 분리(follow-up 후보).
V2_WEIGHTS = {
    "momentum": 20,
    "quality": 20,
    "value": 20,
    "technical": 10,
    "news": 10,
    "supply_demand": 20,
    "sector_momentum": 10,
}

# ─── v2 기본값 (데이터 없을 때) ──────────────────────────────────
V2_NEUTRAL = {
    "momentum": 10.0,
    "quality": 10.0,
    "value": 10.0,
    "technical": 5.0,
    "news": 5.0,
    "supply_demand": 10.0,
    "sector_momentum": 5.0,
}


def score_candidate(
    candidate: EnrichedCandidate,
    benchmark_prices: list[DailyPrice] | None = None,
    *,
    is_bull: bool = False,
) -> QuantScore:
    """Phase 3: v2 잠재력 기반 스코어링.

    Args:
        candidate: 보강된 후보 종목.
        benchmark_prices: KOSPI 벤치마크 일봉 (상대 모멘텀 — v2 미사용 인자, 호환 유지).
        is_bull: 강세장 여부 (RSI 70-80 페널티 면제). v3 macro size_multiplier 환산.

    Returns:
        QuantScore with 7 subscores.
    """
    prices = candidate.daily_prices

    # 데이터 부족 시 중립 점수 반환
    if len(prices) < 20:
        return _neutral_score(candidate, reason=f"Insufficient data: {len(prices)} days")

    momentum = _momentum_score(
        prices, benchmark_prices, is_bull=is_bull, consensus=candidate.consensus
    )
    quality = _quality_score(candidate)
    value = _value_score(candidate)
    technical = _technical_score(prices)
    news = _news_score(candidate)
    supply_demand = _supply_demand_score(candidate)
    sector_momentum = _sector_momentum_score(candidate)

    total = momentum + quality + value + technical + news + supply_demand + sector_momentum
    total = max(0.0, min(100.0, total))

    return QuantScore(
        stock_code=candidate.master.stock_code,
        stock_name=candidate.master.stock_name,
        total_score=round(total, 1),
        momentum_score=round(momentum, 1),
        quality_score=round(quality, 1),
        value_score=round(value, 1),
        technical_score=round(technical, 1),
        news_score=round(news, 1),
        supply_demand_score=round(supply_demand, 1),
        sector_momentum_score=round(sector_momentum, 1),
    )


# ─── Sub-factor Scoring ─────────────────────────────────────────


def _momentum_score(
    prices: list[DailyPrice],
    benchmark: list[DailyPrice] | None,
    *,
    is_bull: bool = False,
    consensus: object | None = None,
) -> float:
    """모멘텀 점수 (0-20): RSI + 가격 모멘텀 + 눌림목 + Earnings Revision."""
    if len(prices) < 20:
        return V2_NEUTRAL["momentum"]

    score = 0.0
    closes = [p.close_price for p in prices]

    # 1. RSI 기반 (0-5): Regime 연동 — BULL에서 70-80은 페널티 없음
    rsi = _compute_rsi(closes, period=14)
    if rsi is not None:
        if 40 <= rsi <= 70:
            score += 5.0
        elif 70 < rsi <= 80:
            score += 5.0 if is_bull else 3.0  # BULL: 강한 추세, 그 외: 모멘텀 인정
        elif 30 <= rsi < 40:
            score += 3.5
        elif rsi < 30:
            score += 4.0  # 과매도 = 반등 잠재력
        else:
            score += 1.0  # 극단 과매수 (>80)

    # 2. 6개월 상대 모멘텀 (0-5)
    if len(closes) >= 120:
        mom_6m = (closes[-1] / closes[-120] - 1) * 100
        score += _linear_map(mom_6m, -20, 30, 0, 5)
    elif len(closes) >= 60:
        mom_3m = (closes[-1] / closes[-60] - 1) * 100
        score += _linear_map(mom_3m, -15, 20, 0, 5)

    # 3. 1개월 단기 모멘텀 (0-5)
    if len(closes) >= 20:
        mom_1m = (closes[-1] / closes[-20] - 1) * 100
        score += _linear_map(mom_1m, -10, 15, 0, 5)

    # 4. 눌림목/추세 감지 (0-5): 6M↑ + 1M↓ = 눌림목, 6M↑ + 1M↑ = 추세 지속
    if len(closes) >= 120:
        mom_6m = (closes[-1] / closes[-120] - 1) * 100
        mom_1m = (closes[-1] / closes[-20] - 1) * 100
        if mom_6m > 5 and mom_1m < -3:
            score += 5.0  # 눌림목 보너스
        elif mom_6m > 0 and mom_1m < 0:
            score += 2.5
        elif mom_6m > 10 and mom_1m > 3:
            score += 3.5  # 추세 지속 보너스 (꾸준한 상승)

    # 5. Earnings Revision 보너스 (0-2): EPS 상향 시 추가 점수
    if consensus is not None:
        eps_rev = getattr(consensus, "eps_revision_pct", None)
        if eps_rev is not None:
            if eps_rev >= 10:
                score += 2.0  # EPS 10%+ 상향
            elif eps_rev >= 5:
                score += 1.0  # EPS 5%+ 상향

    return min(20.0, score)


def _quality_score(candidate: EnrichedCandidate) -> float:
    """품질 점수 (0-20): ROE + 재무 건전성.

    Forward 컨센서스 우선, 없으면 trailing 사용.
    """
    ft = candidate.financial_trend
    cons = candidate.consensus
    if not ft:
        return V2_NEUTRAL["quality"]

    score = 0.0

    # ROE (0-10): forward ROE 우선
    effective_roe = None
    if cons and cons.forward_roe is not None:
        effective_roe = cons.forward_roe
    elif ft.roe is not None:
        effective_roe = ft.roe

    if effective_roe is not None:
        if effective_roe >= 15:
            score += 10.0
        elif effective_roe >= 10:
            score += 8.0
        elif effective_roe >= 5:
            score += 5.0
        elif effective_roe >= 0:
            score += 2.0
        else:
            score += 0.0  # 적자
    else:
        score += 5.0  # 데이터 없음 → 중립 (성장주 ROE 미제공 보정)

    # PBR 기반 자산 품질 (0-5): 섹터 상대평가 우선, 폴백 절대평가
    if candidate.sector_pbr_pctile is not None:
        score += _pctile_to_score_5(candidate.sector_pbr_pctile, floor=1.0)
    elif ft.pbr is not None and ft.pbr > 0:
        if ft.pbr < 1.0:
            score += 5.0
        elif ft.pbr < 2.0:
            score += 3.0
        elif ft.pbr < 4.0:
            score += 1.5
        else:
            score += 1.0

    # PER 안정성 (0-5): 섹터 상대평가 우선, 폴백 절대평가
    if candidate.sector_per_pctile is not None:
        score += _pctile_to_score_5(candidate.sector_per_pctile, floor=0.5)
    else:
        effective_per = None
        if cons and cons.forward_per is not None and cons.forward_per > 0:
            effective_per = cons.forward_per
        elif ft.per is not None and ft.per > 0:
            effective_per = ft.per

        if effective_per is not None:
            if 5 <= effective_per <= 15:
                score += 5.0
            elif 3 <= effective_per <= 25:
                score += 3.0
            elif effective_per <= 50:
                score += 1.0
            else:
                score += 0.5

    return min(20.0, score)


def _value_score(candidate: EnrichedCandidate) -> float:
    """가치 점수 (0-20): PER 할인 + PBR 평가.

    Forward PER 컨센서스 우선, 없으면 trailing PER 사용.
    """
    ft = candidate.financial_trend
    snap = candidate.snapshot
    cons = candidate.consensus
    if not ft:
        return V2_NEUTRAL["value"]

    score = 0.0

    # PER 할인 (0-10): 섹터 상대평가 우선, 폴백 절대평가
    if candidate.sector_per_pctile is not None:
        score += _pctile_to_score_10(candidate.sector_per_pctile)
    else:
        effective_per = None
        if cons and cons.forward_per is not None and cons.forward_per > 0:
            effective_per = cons.forward_per
        elif ft.per is not None and ft.per > 0:
            effective_per = ft.per

        if effective_per is not None:
            if effective_per < 8:
                score += 10.0
            elif effective_per < 12:
                score += 7.0
            elif effective_per < 15:
                score += 5.5
            elif effective_per < 20:
                score += 4.0
            elif effective_per < 30:
                score += 2.5
            elif effective_per < 50:
                score += 2.0
            else:
                score += 1.5

    # PBR 평가 (0-5): 섹터 상대평가 우선, 폴백 절대평가
    if candidate.sector_pbr_pctile is not None:
        score += _pctile_to_score_5(candidate.sector_pbr_pctile, floor=1.0)
    elif ft.pbr is not None and ft.pbr > 0:
        if ft.pbr < 0.7:
            score += 5.0
        elif ft.pbr < 1.0:
            score += 4.0
        elif ft.pbr < 1.5:
            score += 2.5
        elif ft.pbr < 3.0:
            score += 1.5
        else:
            score += 1.0

    # 52주 고점 대비 (0-5): 고점 근접 = 강한 추세 보상
    if snap and snap.high_52w and snap.price:
        drawdown = (snap.price / snap.high_52w - 1) * 100
        if drawdown < -30:
            score += 1.5  # 추세 하락
        elif drawdown < -15:
            score += 3.5  # 큰 할인
        elif drawdown < -5:
            score += 4.0  # 적절한 조정
        else:
            score += 5.0  # 고점 근접 = 상승 추세 확인

    return min(20.0, score)


def _technical_score(prices: list[DailyPrice]) -> float:
    """기술 점수 (0-10): 이평선 + 거래량 패턴."""
    if len(prices) < 20:
        return V2_NEUTRAL["technical"]

    score = 0.0
    closes = [p.close_price for p in prices]
    volumes = [p.volume for p in prices]

    # 이평선 정배열 (0-5): 5MA > 20MA > 60MA
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    current = closes[-1]

    if current > ma5 > ma20:
        score += 5.0  # 정배열 + 가격 위
    elif current > ma20:
        score += 3.0  # MA20 위
    elif current > ma5:
        score += 1.5

    # 거래량 증가 추세 (0-5): 최근 5일 vs 20일 평균
    if len(volumes) >= 20:
        avg_vol_20 = sum(volumes[-20:]) / 20
        avg_vol_5 = sum(volumes[-5:]) / 5
        if avg_vol_20 > 0:
            vol_ratio = avg_vol_5 / avg_vol_20
            if vol_ratio > 2.0:
                score += 5.0
            elif vol_ratio > 1.5:
                score += 4.0
            elif vol_ratio > 1.2:
                score += 3.0  # 대형주 거래량 증가 반영
            elif vol_ratio > 1.0:
                score += 2.0
            else:
                score += 0.5

    return min(10.0, score)


def _news_score(candidate: EnrichedCandidate) -> float:
    """뉴스 점수 (0-10): 감성 모멘텀."""
    sentiment = candidate.news_sentiment_avg
    if sentiment is None:
        return V2_NEUTRAL["news"]

    # sentiment_score: 0=극부정, 50=중립, 100=극긍정
    return _linear_map(sentiment, 20, 80, 0, 10)


def _sector_momentum_score(candidate: EnrichedCandidate) -> float:
    """섹터 모멘텀 점수 (0-10): 섹터 20일 평균 수익률."""
    sector_avg = candidate.sector_avg_return_20d
    if sector_avg is None:
        return V2_NEUTRAL["sector_momentum"]
    return _linear_map(sector_avg, -5.0, 15.0, 0.0, 10.0)


def _supply_demand_score(candidate: EnrichedCandidate) -> float:
    """수급 점수 (0-20): 외인/기관 매수 + 외인 비율 추세."""
    it = candidate.investor_trading
    if not it:
        return V2_NEUTRAL["supply_demand"]

    score = 0.0

    # 외인 순매수 (0-6)
    if it.foreign_net_buy_sum > 0:
        score += min(6.0, 3.0 + it.foreign_net_buy_sum / 5e9 * 3.0)
    elif it.foreign_net_buy_sum < 0:
        score += max(0.0, 3.0 + it.foreign_net_buy_sum / 5e9 * 3.0)
    else:
        score += 3.0  # 중립

    # 기관 순매수 (0-8)
    if it.institution_net_buy_sum > 0:
        score += min(8.0, 4.0 + it.institution_net_buy_sum / 5e9 * 4.0)
    elif it.institution_net_buy_sum < 0:
        score += max(0.0, 4.0 + it.institution_net_buy_sum / 5e9 * 4.0)
    else:
        score += 4.0

    # 외인 보유비율 추세 (0-6)
    if it.foreign_ratio_trend is not None:
        if it.foreign_ratio_trend > 1.0:
            score += 6.0
        elif it.foreign_ratio_trend > 0.3:
            score += 4.5
        elif it.foreign_ratio_trend > 0:
            score += 3.0
        elif it.foreign_ratio_trend > -0.5:
            score += 1.5
        else:
            score += 0.0
    else:
        score += 3.0  # 중립

    return min(20.0, score)


# ─── Helpers ─────────────────────────────────────────────────────


def _compute_rsi(closes: list[int | float], period: int = 14) -> float | None:
    """RSI 계산."""
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0, delta))
        losses.append(max(0, -delta))

    # 초기 평균
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # EMA 방식
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _linear_map(
    value: float, in_min: float, in_max: float, out_min: float, out_max: float
) -> float:
    """선형 매핑: value를 [in_min, in_max] → [out_min, out_max]로 변환."""
    clamped = max(in_min, min(in_max, value))
    if in_max == in_min:
        return (out_min + out_max) / 2
    ratio = (clamped - in_min) / (in_max - in_min)
    return out_min + ratio * (out_max - out_min)


def _pctile_to_score_5(pctile: float, floor: float = 1.0) -> float:
    """섹터 백분위(0=최저) → 0~5점. 낮은 PBR/PER일수록 높은 점수."""
    if pctile <= 20:
        return 5.0
    if pctile <= 40:
        return 3.5
    if pctile <= 60:
        return 2.5
    if pctile <= 80:
        return 1.5
    return floor


def _pctile_to_score_10(pctile: float) -> float:
    """섹터 백분위(0=최저) → 0~10점. PER 할인 평가용."""
    if pctile <= 10:
        return 10.0
    if pctile <= 25:
        return 7.0
    if pctile <= 40:
        return 5.5
    if pctile <= 55:
        return 4.0
    if pctile <= 70:
        return 2.5
    if pctile <= 85:
        return 2.0
    return 1.5


def _neutral_score(candidate: EnrichedCandidate, reason: str = "") -> QuantScore:
    """데이터 부족 시 중립 점수."""
    neutral_total = sum(V2_NEUTRAL.values())
    return QuantScore(
        stock_code=candidate.master.stock_code,
        stock_name=candidate.master.stock_name,
        total_score=neutral_total,
        momentum_score=V2_NEUTRAL["momentum"],
        quality_score=V2_NEUTRAL["quality"],
        value_score=V2_NEUTRAL["value"],
        technical_score=V2_NEUTRAL["technical"],
        news_score=V2_NEUTRAL["news"],
        supply_demand_score=V2_NEUTRAL["supply_demand"],
        sector_momentum_score=V2_NEUTRAL["sector_momentum"],
        is_valid=False,
        invalid_reason=reason,
    )


__all__ = [
    "V2_NEUTRAL",
    "V2_WEIGHTS",
    "QuantScore",
    "score_candidate",
]
