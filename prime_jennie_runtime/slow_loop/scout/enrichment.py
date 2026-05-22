"""결정론 선정 — Phase 2 데이터 보강 (Enrichment).

v2 `prime_jennie/services/scout/enrichment.py` 의 포팅 (2026-05-22, Phase 1 a).
v2 는 종목별 동기 쿼리 + KIS 스냅샷 병렬 수집이었다. v3 포팅은:
  - v3 postgres 테이블(daily_prices / stock_investor_tradings / stock_fundamentals /
    stock_consensus / news_sentiments / stock_masters)에서 async SQLAlchemy 로드.
  - 종목별 N회 쿼리 대신 universe 전체 batch 쿼리 (`= ANY(:codes)`) — 집계 로직은
    v2 와 동일, fetch 만 batch. 200종목 × 5테이블 = 1000쿼리 → 6쿼리.
  - KIS 실시간 스냅샷 호출은 포팅하지 않음 (선정 경로 외부 의존 최소화) —
    `StockSnapshot` 은 daily_prices 최신행에서 합성 (price=마지막 종가,
    high_52w=로드 윈도우 내 최고가 — 윈도우 제한 근사, docstring 명시).
  - news factor: v2 `news_sentiment_avg` (0-100) → v3 `news_sentiments.score`
    (-1~1 척도) 를 0-100 으로 변환 후 평균 (quant.py `_news_score` 무수정 유지).

에러 격리: 테이블별 쿼리 실패 시 해당 필드만 비고 진행 (fail-open) — v2 동일.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# v2 enrichment.py 기본 윈도우 — 일봉 150일, 수급 60일, 뉴스 14일, 컨센서스 30일.
DAILY_PRICE_LOOKBACK_DAYS = 150
INVESTOR_LOOKBACK_DAYS = 60
NEWS_LOOKBACK_DAYS = 14
CONSENSUS_HISTORY_DAYS = 30


# =====================================================================
# 입력 데이터 타입 — v2 domain/stock.py + enrichment.py 모델의 v3 로컬 포팅
# =====================================================================


class DailyPrice(BaseModel):
    """일별 OHLCV — v2 domain/stock.py DailyPrice 포팅 (StockCode→str)."""

    stock_code: str
    price_date: date
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    change_pct: float | None = None


class StockMaster(BaseModel):
    """종목 마스터 — v2 domain/stock.py StockMaster 포팅.

    sector_group 은 v3 stock_masters 가 text 컬럼이므로 ``str | None`` (v2 는
    SectorGroup enum — quant.py 는 dict 키로만 쓰므로 str 로 충분).
    """

    stock_code: str
    stock_name: str
    market: str = "KOSPI"
    market_cap: int | None = None
    sector_naver: str | None = None
    sector_group: str | None = None
    is_active: bool = True


class StockSnapshot(BaseModel):
    """현재가 스냅샷 — v2 는 KIS API. v3 포팅은 daily_prices 최신행에서 합성.

    quant.py 가 실제로 쓰는 필드는 ``price`` / ``high_52w`` 뿐 (`_value_score`
    52주 고점 대비). ``high_52w`` 는 로드된 일봉 윈도우(150일) 내 최고가 —
    실제 52주(250영업일)보다 짧은 근사. v2 는 KIS 가 정확한 52주가를 줬다.
    """

    stock_code: str
    price: int
    change_pct: float = 0.0
    high_52w: int | None = None
    low_52w: int | None = None


class InvestorTradingSummary(BaseModel):
    """수급 데이터 요약 — v2 enrichment.py InvestorTradingSummary 포팅."""

    foreign_net_buy_sum: float = 0.0
    institution_net_buy_sum: float = 0.0
    foreign_holding_ratio: float | None = None
    foreign_ratio_trend: float | None = None  # 최근 10일 vs 이전 10일


class FinancialTrend(BaseModel):
    """재무 트렌드 — v2 enrichment.py FinancialTrend 포팅."""

    per: float | None = None
    pbr: float | None = None
    roe: float | None = None


class ConsensusInfo(BaseModel):
    """Forward 컨센서스 — v2 enrichment.py ConsensusInfo 포팅."""

    forward_per: float | None = None
    forward_eps: float | None = None
    forward_roe: float | None = None
    target_price: int | None = None
    analyst_count: int | None = None
    investment_opinion: float | None = None
    eps_revision_pct: float | None = None  # 최근 30일 forward EPS 변화율 (%)


class EnrichedCandidate(BaseModel):
    """Phase 2 출력 — 팩터 분석에 필요한 모든 데이터. v2 동명 모델 포팅."""

    master: StockMaster
    snapshot: StockSnapshot | None = None
    daily_prices: list[DailyPrice] = []
    news_sentiment_avg: float | None = None
    investor_trading: InvestorTradingSummary | None = None
    financial_trend: FinancialTrend | None = None
    consensus: ConsensusInfo | None = None
    sector_avg_return_20d: float | None = None
    sector_pbr_pctile: float | None = None
    sector_per_pctile: float | None = None


# =====================================================================
# v3 DB batch 로더
# =====================================================================


def _sentiment_to_0_100(score: float) -> float:
    """v3 news_sentiments.score (-1~1 척도) → v2 _news_score 가 기대하는 0-100.

    실측 분포 min -0.6 / max 0.7 / avg 0.38. 선형 변환 (score+1)/2*100 후 클램프.
    """
    return max(0.0, min(100.0, (score + 1.0) / 2.0 * 100.0))


async def enrich_universe(
    engine: AsyncEngine,
    universe: list[str],
    *,
    as_of: date,
) -> dict[str, EnrichedCandidate]:
    """universe 종목 전체를 batch 쿼리로 보강해 EnrichedCandidate dict 반환.

    Args:
        engine: v3 postgres AsyncEngine.
        universe: 6자리 종목코드 리스트.
        as_of: 기준일 (이 날짜 이하 데이터만 사용 — lookahead 방지).

    Returns:
        {stock_code: EnrichedCandidate}. 일부 테이블 쿼리가 실패해도 해당 필드만
        비우고 진행 (fail-open).
    """
    if not universe:
        return {}

    masters = await _load_masters(engine, universe)
    if not masters:
        logger.warning("enrich_universe: stock_masters 에서 universe 종목 0건")
        return {}

    codes = list(masters.keys())
    prices = await _load_daily_prices(engine, codes, as_of=as_of)
    trading = await _load_investor_trading(engine, codes, as_of=as_of)
    fundamentals = await _load_fundamentals(engine, codes, as_of=as_of)
    consensus = await _load_consensus(engine, codes, as_of=as_of)
    news = await _load_news_sentiment(engine, codes, as_of=as_of)

    result: dict[str, EnrichedCandidate] = {}
    for code, master in masters.items():
        candidate = EnrichedCandidate(master=master)
        candidate.daily_prices = prices.get(code, [])
        candidate.snapshot = _build_snapshot(code, candidate.daily_prices)
        candidate.investor_trading = trading.get(code)
        candidate.financial_trend = fundamentals.get(code)
        candidate.consensus = consensus.get(code)
        candidate.news_sentiment_avg = news.get(code)
        result[code] = candidate

    logger.info(
        "enrich_universe: %d candidates (prices=%d, trading=%d, fund=%d, consensus=%d, news=%d)",
        len(result),
        sum(1 for c in result.values() if c.daily_prices),
        sum(1 for c in result.values() if c.investor_trading),
        sum(1 for c in result.values() if c.financial_trend),
        sum(1 for c in result.values() if c.consensus),
        sum(1 for c in result.values() if c.news_sentiment_avg is not None),
    )
    return result


def _build_snapshot(code: str, prices: list[DailyPrice]) -> StockSnapshot | None:
    """daily_prices 최신행 + 윈도우 최고/최저가로 StockSnapshot 합성."""
    if not prices:
        return None
    latest = prices[-1]
    highs = [p.high_price for p in prices if p.high_price > 0]
    lows = [p.low_price for p in prices if p.low_price > 0]
    change_pct = 0.0
    if len(prices) >= 2 and prices[-2].close_price > 0:
        change_pct = (latest.close_price / prices[-2].close_price - 1) * 100
    return StockSnapshot(
        stock_code=code,
        price=latest.close_price,
        change_pct=round(change_pct, 2),
        high_52w=max(highs) if highs else None,
        low_52w=min(lows) if lows else None,
    )


async def _load_masters(engine: AsyncEngine, universe: list[str]) -> dict[str, StockMaster]:
    """stock_masters 에서 universe 종목 마스터 로드."""
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT stock_code, stock_name, market, market_cap, "
                    "sector_naver, sector_group, is_active "
                    "FROM stock_masters WHERE stock_code = ANY(:codes)"
                ),
                {"codes": universe},
            )
            rows = res.mappings().all()
    except Exception:
        logger.exception("_load_masters 실패 — 빈 dict 반환 (fail-open)")
        return {}

    out: dict[str, StockMaster] = {}
    for row in rows:
        try:
            out[row["stock_code"]] = StockMaster(
                stock_code=row["stock_code"],
                stock_name=row["stock_name"],
                market=row["market"] or "KOSPI",
                market_cap=row["market_cap"],
                sector_naver=row["sector_naver"],
                sector_group=row["sector_group"],
                is_active=row["is_active"],
            )
        except Exception as e:  # noqa: BLE001 — 종목별 격리
            logger.warning("master 변환 skip %s: %s", row.get("stock_code"), e)
    return out


async def _load_daily_prices(
    engine: AsyncEngine, codes: list[str], *, as_of: date
) -> dict[str, list[DailyPrice]]:
    """daily_prices 에서 종목별 최근 DAILY_PRICE_LOOKBACK_DAYS 건 (오래된→최신 순)."""
    stmt = text(
        """
        SELECT sub.stock_code, sub.price_date, sub.open_price, sub.high_price,
               sub.low_price, sub.close_price, sub.volume
        FROM unnest(CAST(:codes AS TEXT[])) AS t(stock_code)
        CROSS JOIN LATERAL (
            SELECT stock_code, price_date, open_price, high_price,
                   low_price, close_price, volume
            FROM daily_prices dp
            WHERE dp.stock_code = t.stock_code AND dp.price_date <= :as_of
            ORDER BY dp.price_date DESC
            LIMIT :lookback
        ) AS sub
        ORDER BY sub.stock_code, sub.price_date
        """
    )
    out: dict[str, list[DailyPrice]] = {}
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                stmt,
                {"codes": codes, "as_of": as_of, "lookback": DAILY_PRICE_LOOKBACK_DAYS},
            )
            rows = res.mappings().all()
    except Exception:
        logger.exception("_load_daily_prices 실패 — 빈 dict (fail-open)")
        return {}

    for row in rows:
        try:
            out.setdefault(row["stock_code"], []).append(
                DailyPrice(
                    stock_code=row["stock_code"],
                    price_date=row["price_date"],
                    open_price=int(row["open_price"]),
                    high_price=int(row["high_price"]),
                    low_price=int(row["low_price"]),
                    close_price=int(row["close_price"]),
                    volume=int(row["volume"]),
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("daily_price 변환 skip %s: %s", row.get("stock_code"), e)
    return out


async def _load_investor_trading(
    engine: AsyncEngine, codes: list[str], *, as_of: date
) -> dict[str, InvestorTradingSummary]:
    """stock_investor_tradings 60일 → 외인/기관 순매수 합 + 외인비율 추세.

    v2 enrichment.py 의 집계 로직 동일: foreign/institution net_buy 합산,
    foreign_ratio_trend = 최근 10일 평균 − 이전 10일 평균 (20일 이상일 때만).
    """
    since = as_of - timedelta(days=INVESTOR_LOOKBACK_DAYS * 2)  # 거래일 여유분 포함
    stmt = text(
        """
        SELECT stock_code, trade_date, foreign_net_buy, institution_net_buy,
               foreign_holding_ratio
        FROM stock_investor_tradings
        WHERE stock_code = ANY(:codes) AND trade_date <= :as_of AND trade_date >= :since
        ORDER BY stock_code, trade_date
        """
    )
    by_code: dict[str, list[dict[str, Any]]] = {}
    try:
        async with engine.connect() as conn:
            res = await conn.execute(stmt, {"codes": codes, "as_of": as_of, "since": since})
            for row in res.mappings().all():
                by_code.setdefault(row["stock_code"], []).append(dict(row))
    except Exception:
        logger.exception("_load_investor_trading 실패 — 빈 dict (fail-open)")
        return {}

    out: dict[str, InvestorTradingSummary] = {}
    for code, rows in by_code.items():
        recent = rows[-INVESTOR_LOOKBACK_DAYS:]  # 최근 60거래일
        foreign_sum = sum(float(r["foreign_net_buy"] or 0) for r in recent)
        inst_sum = sum(float(r["institution_net_buy"] or 0) for r in recent)
        ratios = [
            float(r["foreign_holding_ratio"])
            for r in recent
            if r["foreign_holding_ratio"] is not None
        ]
        ratio_trend = None
        if len(ratios) >= 20:
            recent10 = sum(ratios[-10:]) / 10
            prev10 = sum(ratios[-20:-10]) / 10
            ratio_trend = recent10 - prev10
        out[code] = InvestorTradingSummary(
            foreign_net_buy_sum=foreign_sum,
            institution_net_buy_sum=inst_sum,
            foreign_holding_ratio=ratios[-1] if ratios else None,
            foreign_ratio_trend=ratio_trend,
        )
    return out


async def _load_fundamentals(
    engine: AsyncEngine, codes: list[str], *, as_of: date
) -> dict[str, FinancialTrend]:
    """stock_fundamentals 에서 종목별 최신(trade_date<=as_of) per/pbr/roe."""
    stmt = text(
        """
        SELECT DISTINCT ON (stock_code) stock_code, per, pbr, roe
        FROM stock_fundamentals
        WHERE stock_code = ANY(:codes) AND trade_date <= :as_of
        ORDER BY stock_code, trade_date DESC
        """
    )
    out: dict[str, FinancialTrend] = {}
    try:
        async with engine.connect() as conn:
            res = await conn.execute(stmt, {"codes": codes, "as_of": as_of})
            for row in res.mappings().all():
                out[row["stock_code"]] = FinancialTrend(
                    per=row["per"], pbr=row["pbr"], roe=row["roe"]
                )
    except Exception:
        logger.exception("_load_fundamentals 실패 — 빈 dict (fail-open)")
        return {}
    return out


async def _load_consensus(
    engine: AsyncEngine, codes: list[str], *, as_of: date
) -> dict[str, ConsensusInfo]:
    """stock_consensus 최신행 + 30일 전 forward_eps 대비 변화율(eps_revision_pct).

    v2 enrichment.py 와 동일: 최신 forward_per/eps/roe + 30일 이상 과거 행이
    있으면 (latest_eps - old_eps)/abs(old_eps)*100.
    """
    latest_stmt = text(
        """
        SELECT DISTINCT ON (stock_code) stock_code, trade_date, forward_per,
               forward_eps, forward_roe, target_price, analyst_count, investment_opinion
        FROM stock_consensus
        WHERE stock_code = ANY(:codes) AND trade_date <= :as_of
        ORDER BY stock_code, trade_date DESC
        """
    )
    # 30일 이전의 가장 최근 행 (eps_revision 기준점)
    old_stmt = text(
        """
        SELECT DISTINCT ON (stock_code) stock_code, trade_date, forward_eps
        FROM stock_consensus
        WHERE stock_code = ANY(:codes) AND trade_date <= :cutoff
        ORDER BY stock_code, trade_date DESC
        """
    )
    out: dict[str, ConsensusInfo] = {}
    try:
        cutoff = as_of - timedelta(days=CONSENSUS_HISTORY_DAYS)
        async with engine.connect() as conn:
            latest_res = await conn.execute(latest_stmt, {"codes": codes, "as_of": as_of})
            latest_rows = latest_res.mappings().all()
            old_res = await conn.execute(old_stmt, {"codes": codes, "cutoff": cutoff})
            old_rows = old_res.mappings().all()
    except Exception:
        logger.exception("_load_consensus 실패 — 빈 dict (fail-open)")
        return {}

    old_eps: dict[str, tuple[date, float]] = {}
    for row in old_rows:
        if row["forward_eps"] is not None:
            old_eps[row["stock_code"]] = (row["trade_date"], float(row["forward_eps"]))

    for row in latest_rows:
        code = row["stock_code"]
        info = ConsensusInfo(
            forward_per=row["forward_per"],
            forward_eps=row["forward_eps"],
            forward_roe=row["forward_roe"],
            target_price=row["target_price"],
            analyst_count=row["analyst_count"],
            investment_opinion=row["investment_opinion"],
        )
        if row["forward_eps"] is not None and code in old_eps:
            old_date, old_val = old_eps[code]
            if old_val != 0 and old_date != row["trade_date"]:
                info.eps_revision_pct = (
                    (float(row["forward_eps"]) - old_val) / abs(old_val) * 100
                )
        out[code] = info
    return out


async def _load_news_sentiment(
    engine: AsyncEngine, codes: list[str], *, as_of: date
) -> dict[str, float]:
    """news_sentiments 14일 평균 → v2 0-100 척도로 변환.

    v3 news_sentiments.score 는 -1~1 척도라 _sentiment_to_0_100 으로 변환 후
    종목별 평균. quant.py `_news_score` 는 0-100 가정이므로 무수정 유지.
    """
    since = as_of - timedelta(days=NEWS_LOOKBACK_DAYS)
    stmt = text(
        """
        SELECT ticker, score
        FROM news_sentiments
        WHERE ticker = ANY(:codes)
          AND analyzed_at >= :since AND analyzed_at < :until
        """
    )
    by_code: dict[str, list[float]] = {}
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                stmt,
                {
                    "codes": codes,
                    "since": since,
                    "until": as_of + timedelta(days=1),
                },
            )
            for row in res.mappings().all():
                by_code.setdefault(row["ticker"], []).append(
                    _sentiment_to_0_100(float(row["score"]))
                )
    except Exception:
        logger.exception("_load_news_sentiment 실패 — 빈 dict (fail-open)")
        return {}

    return {code: sum(vals) / len(vals) for code, vals in by_code.items() if vals}


__all__ = [
    "ConsensusInfo",
    "DailyPrice",
    "EnrichedCandidate",
    "FinancialTrend",
    "InvestorTradingSummary",
    "StockMaster",
    "StockSnapshot",
    "enrich_universe",
]
