"""5분 주기 asyncio 루프: market snapshot을 읽어 IntradayRiskThrottle 업데이트.

Track C Stage 2. v2의 `_check_intraday_risk` 반복 호출 로직을 async로 포팅.

구조:
  - `fetch_snapshot` callable (async): (kospi_pct, vix, sox_pct, council_mult) 튜플 반환
  - `IntradayRiskThrottle.update()` 호출
  - level 변경 시 notifier.publish(RiskLevelChangeNotification(...))
  - `asyncio.sleep(interval_sec)` 주기로 반복
  - `stop()` 또는 CancelledError로 종료
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from prime_jennie_runtime.fast_loop.risk_throttle import IntradayRiskThrottle
from prime_jennie_runtime.fast_loop.schemas import RiskLevelChangeNotification
from prime_jennie_runtime.infra.redis_streams import TypedStreamPublisher

logger = logging.getLogger(__name__)

# fetch_snapshot(): (kospi_pct, vix, sox_pct, council_mult)
SnapshotFetcher = Callable[[], Awaitable[tuple[float, float | None, float | None, float]]]


async def run_risk_updater(
    throttle: IntradayRiskThrottle,
    fetch_snapshot: SnapshotFetcher,
    interval_sec: int = 300,
    notifier: TypedStreamPublisher[RiskLevelChangeNotification] | None = None,
    clock: Callable[[], datetime] | None = None,
    max_iterations: int | None = None,
) -> int:
    """5분 주기로 throttle을 업데이트. level 변경 시 notifier로 발행.

    Args:
        throttle: IntradayRiskThrottle 인스턴스
        fetch_snapshot: 현재 market snapshot 조회 콜백
        interval_sec: 업데이트 주기 (기본 300s = 5분)
        notifier: 선택적 발행자. None이면 로그만
        clock: 현재 시각 반환 (알림 ts용)
        max_iterations: 테스트용 반복 제한. None이면 무한

    Returns:
        실행된 iteration 횟수
    """
    clock_fn = clock if clock is not None else datetime.now
    iterations = 0
    _now = clock_fn  # alias

    while True:
        if max_iterations is not None and iterations >= max_iterations:
            return iterations

        try:
            snapshot = await fetch_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("risk_updater: fetch_snapshot failed")
            await _sleep_or_cancel(interval_sec)
            iterations += 1
            continue

        kospi_pct, vix, sox_pct, council_mult = snapshot

        try:
            result = await throttle.update(
                kospi_pct=kospi_pct,
                vix=vix,
                sox_pct=sox_pct,
                council_mult=council_mult,
                now=_now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("risk_updater: throttle.update failed")
            await _sleep_or_cancel(interval_sec)
            iterations += 1
            continue

        logger.info(
            "INTRADAY_RISK: level=%s, KOSPI=%.2f%%, VIX=%s, SOX=%s, "
            "council=%.2f, intraday=%.2f, final=%.2f",
            result.level,
            kospi_pct,
            f"{vix:.1f}" if vix is not None else "N/A",
            f"{sox_pct:+.1f}%" if sox_pct is not None else "N/A",
            council_mult,
            result.intraday_multiplier,
            result.council_final,
        )

        # 레벨 변경 시 알림
        if result.level_changed and notifier is not None:
            try:
                await notifier.publish(
                    RiskLevelChangeNotification(
                        prev_level=result.prev_level,
                        new_level=result.level,
                        prev_multiplier=_level_multiplier(result.prev_level),
                        new_multiplier=result.intraday_multiplier,
                        kospi_change_pct=kospi_pct,
                        vix=vix,
                        sox_change_pct=sox_pct,
                        ts=_now(),
                    )
                )
            except Exception:
                logger.exception("risk_updater: notifier publish failed")

        iterations += 1

        if max_iterations is not None and iterations >= max_iterations:
            return iterations

        await _sleep_or_cancel(interval_sec)


async def _sleep_or_cancel(seconds: int) -> None:
    """sleep wrapper — CancelledError 전파."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        raise


def _level_multiplier(level: str) -> float:
    """알림용 multiplier 조회 — 순환 import 회피 목적의 가벼운 헬퍼."""
    from prime_jennie_runtime.fast_loop.risk_throttle import LEVEL_MULTIPLIER

    return LEVEL_MULTIPLIER.get(level, 1.0)
