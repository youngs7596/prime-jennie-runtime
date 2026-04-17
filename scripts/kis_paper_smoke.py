"""KIS paper account smoke test (Phase 2.8).

영석이 paper 토큰 발급 후 로컬에서 바로 돌릴 수 있는 최소 검증 스크립트.
실 KIS 모의서버를 호출하므로 CI 에서는 돌지 않음 (pytest 미포함).

사용:

    KIS_APP_KEY=... KIS_APP_SECRET=... KIS_ACCOUNT_NO=... \\
    python -m scripts.kis_paper_smoke --ticker 005930

단계:
1. authenticate  — 토큰 발급 + 캐시 저장
2. get_snapshot  — 1건 시세 조회
3. get_daily_prices — 최근 10일 캔들
4. (선택) --order 옵션 시 1주 시장가 매수 → 상태 조회 → 시장가 매도

기본은 dry (주문 생략). 각 단계 성공/실패를 stdout 에 한 줄씩 출력.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass

from prime_jennie_runtime.infra.config import KISConfig
from prime_jennie_runtime.kis_gateway.kis_api import KISApi, KISApiError

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


async def run(ticker: str, do_order: bool) -> list[StepResult]:
    cfg = KISConfig()
    if not (cfg.app_key and cfg.app_secret and cfg.account_no):
        return [
            StepResult(
                "config",
                False,
                "KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 필수",
            )
        ]
    if not cfg.is_paper:
        return [StepResult("config", False, "is_paper=False — smoke 는 paper 에서만 실행")]

    api = KISApi(cfg)
    results: list[StepResult] = []

    try:
        token = await api.authenticate(force=True)
        results.append(StepResult("authenticate", True, detail=f"token={token[:8]}…"))
    except KISApiError as e:
        results.append(StepResult("authenticate", False, str(e)))
        await api.close()
        return results

    try:
        snap = await api.get_snapshot(ticker)
        results.append(
            StepResult("snapshot", True, f"{ticker} price={snap.price:,} vol={snap.volume:,}")
        )
    except KISApiError as e:
        results.append(StepResult("snapshot", False, str(e)))

    try:
        daily = await api.get_daily_prices(ticker, days=10)
        results.append(StepResult("daily_prices", True, f"{len(daily)} candles"))
    except KISApiError as e:
        results.append(StepResult("daily_prices", False, str(e)))

    if do_order:
        results.extend(await _order_cycle(api, ticker))

    await api.close()
    return results


async def _order_cycle(api: KISApi, ticker: str) -> list[StepResult]:
    out: list[StepResult] = []
    try:
        order = await api.place_order(order_type="buy", stock_code=ticker, quantity=1)
        order_no = order.get("order_no", "")
        if not order_no:
            out.append(StepResult("order_buy", False, "no order_no returned"))
            return out
        out.append(StepResult("order_buy", True, f"order_no={order_no}"))
    except KISApiError as e:
        out.append(StepResult("order_buy", False, str(e)))
        return out

    await asyncio.sleep(1.0)

    try:
        status = await api.check_order_status(order_no)
        out.append(StepResult("order_status", status is not None, str(status or "none")))
    except KISApiError as e:
        out.append(StepResult("order_status", False, str(e)))

    try:
        sell = await api.place_order(order_type="sell", stock_code=ticker, quantity=1)
        out.append(StepResult("order_sell", bool(sell.get("order_no")), str(sell)))
    except KISApiError as e:
        out.append(StepResult("order_sell", False, str(e)))
    return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KIS paper account smoke test")
    p.add_argument("--ticker", default="005930", help="조회용 종목 코드 (기본: 삼성전자)")
    p.add_argument("--order", action="store_true", help="1주 시장가 매수/매도까지 수행")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = asyncio.run(run(args.ticker, args.order))
    for r in results:
        status = "OK" if r.ok else "FAIL"
        suffix = f" — {r.detail}" if r.detail else ""
        print(f"[{status}] {r.name}{suffix}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
