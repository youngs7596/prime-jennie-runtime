"""Macro feeder 실구현 — Redis 스냅샷 + Postgres 시장 데이터 + 뉴스 집계.

Phase 2.12 B1~B3. stub 3종을 교체한다.

- RealMarketSnapshotFeeder: Redis `macro:data:snapshot:{date}` + us_market_daily
  + index_daily_prices (volatility 계산) + daily_prices × stock_masters.sector_group (섹터 drops)
- RealKorMacroNewsFeeder: news_articles + news_sentiments 조인, 최근 24h 상위 N건
- RealWsjDigestFeeder: us_market_daily + 매크로 스냅샷 기반 factual digest (단계 1).
  실제 WSJ 본문 요약은 별도 크롤러 (후속 작업).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
from datetime import datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..schemas import IndexPoint, MarketSnapshot, SectorDrop

logger = logging.getLogger(__name__)

MACRO_SNAPSHOT_KEY_PREFIX = "macro:data:snapshot:"
MAX_SNAPSHOT_LOOKBACK_DAYS = 5  # 주말/공휴일 커버
DEFAULT_INDEX_POINT = IndexPoint(close=0.0, change_pct=0.0)


# ---------------------------------------------------------------- helpers


async def _load_macro_snapshot(
    redis_client: aioredis.Redis,
    as_of: datetime,
) -> dict[str, Any]:
    """최근 며칠 (주말/공휴일 감안) 범위에서 가장 최신 snapshot 을 찾는다."""
    target = as_of.date()
    for i in range(MAX_SNAPSHOT_LOOKBACK_DAYS):
        day = target - timedelta(days=i)
        key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{day.isoformat()}"
        raw = await redis_client.get(key)
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("macro snapshot invalid JSON: key=%s", key)
            continue
        return data
    logger.warning("macro snapshot not found within %d days", MAX_SNAPSHOT_LOOKBACK_DAYS)
    return {}


def _as_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- MarketSnapshot


async def _fetch_us_index(engine: AsyncEngine, ticker: str) -> IndexPoint:
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "SELECT close_price, change_pct FROM us_market_daily "
                "WHERE ticker = :t ORDER BY price_date DESC LIMIT 1"
            ),
            {"t": ticker},
        )
        row = res.mappings().one_or_none()
    if row is None:
        return DEFAULT_INDEX_POINT.model_copy()
    return IndexPoint(
        close=_as_float(row["close_price"]),
        change_pct=_as_float(row["change_pct"]) / 100.0,  # DB 는 percent, schema 는 소수
    )


async def _fetch_kospi_vol(engine: AsyncEngine) -> tuple[float, float]:
    """KOSPI 일별 change_pct → 연율화 변동성 (20d, 60d)."""
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "SELECT change_pct FROM index_daily_prices "
                "WHERE index_code = 'KOSPI' AND change_pct IS NOT NULL "
                "ORDER BY price_date DESC LIMIT 60"
            )
        )
        rows = res.all()
    changes = [_as_float(r[0]) / 100.0 for r in rows]  # DB percent → 소수
    if len(changes) < 5:
        return 0.0, 0.0
    vol20 = statistics.stdev(changes[:20]) * math.sqrt(252) if len(changes) >= 5 else 0.0
    vol60 = statistics.stdev(changes[:60]) * math.sqrt(252) if len(changes) >= 20 else vol20
    return vol20, vol60


async def _fetch_sector_drops(engine: AsyncEngine, as_of: datetime) -> list[SectorDrop]:
    """오늘 (또는 최신 장일) 섹터별 평균 change_pct 중 -2% 이하를 SectorDrop 으로 리턴."""
    target = as_of.date()
    # 가장 최근 장일 (주말 대응)
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "SELECT DISTINCT trade_date FROM daily_prices "
                "WHERE trade_date <= :t ORDER BY trade_date DESC LIMIT 1"
            ),
            {"t": target},
        )
        row = res.one_or_none()
        if row is None:
            return []
        latest = row[0]
        res = await conn.execute(
            text(
                "SELECT sm.sector_group AS sector, AVG(dp.change_pct) AS avg_pct "
                "FROM daily_prices dp "
                "JOIN stock_masters sm ON sm.stock_code = dp.stock_code "
                "WHERE dp.trade_date = :d AND dp.change_pct IS NOT NULL "
                "AND sm.sector_group IS NOT NULL "
                "GROUP BY sm.sector_group "
                "HAVING AVG(dp.change_pct) <= -2.0 "
                "ORDER BY avg_pct ASC LIMIT 10"
            ),
            {"d": latest},
        )
        rows = res.mappings().all()
    return [
        SectorDrop(sector=r["sector"], change_pct=_as_float(r["avg_pct"]) / 100.0) for r in rows
    ]


class RealMarketSnapshotFeeder:
    """Redis snapshot + Postgres 시장 데이터 기반 실제 MarketSnapshot."""

    def __init__(self, engine: AsyncEngine, redis_client: aioredis.Redis) -> None:
        self._engine = engine
        self._redis = redis_client

    async def fetch(self, as_of: datetime) -> MarketSnapshot:
        snap = await _load_macro_snapshot(self._redis, as_of)

        # 국내 (Redis snapshot — KOSPI/KOSDAQ/VIX/USD-KRW/외국인 net 등 있음)
        kospi = IndexPoint(
            close=_as_float(snap.get("kospi_index")),
            change_pct=_as_float(snap.get("kospi_change_pct")) / 100.0,
        )
        kosdaq = IndexPoint(
            close=_as_float(snap.get("kosdaq_index")),
            change_pct=_as_float(snap.get("kosdaq_change_pct")) / 100.0,
        )

        # 해외 (us_market_daily — SP500/NASDAQ 는 있음; Nikkei/HSI 는 현재 수집 안 됨)
        sp500 = await _fetch_us_index(self._engine, "SP500")
        nasdaq = await _fetch_us_index(self._engine, "NASDAQ")
        # 수집 안 된 지수는 0 으로 — Macro prompt 의 closed 조건은 한국 기준이라 치명적 영향 없음
        nikkei = DEFAULT_INDEX_POINT.model_copy()
        hsi = DEFAULT_INDEX_POINT.model_copy()

        # 환율 (USD/KRW 은 Redis 에 있지만 종종 None — default fallback)
        usd_krw = _as_float(snap.get("usd_krw"), 1350.0)
        usd_jpy = _as_float(snap.get("usd_jpy"), 150.0)

        # 원자재 (미수집 — neutral placeholder)
        crude = 80.0
        gold = 2300.0

        # VIX
        vix = _as_float(snap.get("vix"), 20.0)

        # 변동성 계산
        vol20, vol60 = await _fetch_kospi_vol(self._engine)

        # 섹터 drops
        sector_drops = await _fetch_sector_drops(self._engine, as_of)

        return MarketSnapshot(
            as_of=as_of,
            kospi=kospi,
            kosdaq=kosdaq,
            sp500=sp500,
            nasdaq=nasdaq,
            nikkei=nikkei,
            hsi=hsi,
            usd_krw=usd_krw,
            usd_krw_change_pct=0.0,  # 전일 대비 계산은 후속
            usd_jpy=usd_jpy,
            crude_oil=crude,
            crude_oil_change_pct=0.0,
            gold=gold,
            gold_change_pct=0.0,
            vix=vix,
            vix_prev=None,
            vkospi=None,
            kospi_20d_vol=vol20,
            kospi_60d_vol=vol60,
            major_sector_drops=sector_drops,
        )


# ---------------------------------------------------------------- KorMacroNews


class RealKorMacroNewsFeeder:
    """news_articles + news_sentiments 에서 최근 24h 매크로성 뉴스 상위 N건."""

    def __init__(self, engine: AsyncEngine, top_n: int = 10) -> None:
        self._engine = engine
        self._top_n = top_n

    async def fetch(self, as_of: datetime) -> str:
        since = as_of - timedelta(hours=24)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "SELECT na.title, na.published_at, ns.score, ns.label "
                    "FROM news_articles na "
                    "LEFT JOIN news_sentiments ns ON ns.article_id = na.article_id "
                    "WHERE na.published_at >= :since "
                    "ORDER BY ABS(COALESCE(ns.score, 0)) DESC, na.published_at DESC "
                    "LIMIT :n"
                ),
                {"since": since, "n": self._top_n},
            )
            rows = res.mappings().all()
        if not rows:
            return "- (최근 24h 국내 매크로 뉴스 없음)"
        lines = []
        for r in rows:
            score = r["score"]
            label = r["label"] or "-"
            title = (r["title"] or "").strip().replace("\n", " ")[:120]
            tag = f"[{label} {score:+.2f}]" if score is not None else f"[{label}]"
            lines.append(f"- {title} {tag}")
        return "\n".join(lines)


# ---------------------------------------------------------------- WsjDigest


class RealWsjDigestFeeder:
    """단계 1 — us_market_daily + Redis macro snapshot 기반 factual digest.

    digest_id 는 본문 내용 SHA-256 (재현성). 실 WSJ 본문 요약은 후속 크롤러.
    """

    def __init__(self, engine: AsyncEngine, redis_client: aioredis.Redis) -> None:
        self._engine = engine
        self._redis = redis_client

    async def fetch(self, as_of: datetime) -> tuple[str, str, str]:
        snap = await _load_macro_snapshot(self._redis, as_of)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "SELECT ticker, close_price, change_pct FROM us_market_daily "
                    "WHERE (ticker, price_date) IN ( "
                    "  SELECT ticker, MAX(price_date) FROM us_market_daily GROUP BY ticker "
                    ") ORDER BY ticker"
                )
            )
            rows = res.mappings().all()
        us_lines = []
        for r in rows:
            us_lines.append(
                f"- {r['ticker']}: {_as_float(r['close_price']):.2f} "
                f"({_as_float(r['change_pct']):+.2f}%)"
            )
        vix_regime = snap.get("vix_regime", "unknown")
        vix_val = _as_float(snap.get("vix"), 0.0)
        summary = (
            f"US 장세 요약 — SP500/NASDAQ/SOX/NVDA 등 최신 종가 기반. "
            f"VIX {vix_val:.2f} (regime={vix_regime}). "
            f"상세 매크로 이벤트는 B3 후속 WSJ 크롤러 배선 후 보강."
        )
        headlines = "\n".join(us_lines) if us_lines else "- (US market data unavailable)"
        content = f"{summary}\n{headlines}\n{as_of.date().isoformat()}"
        digest_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return digest_id, summary, headlines
