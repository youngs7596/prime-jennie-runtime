"""Macro / Council 관련 job — 외부 지표 수집, 검증, council trigger.

v2 원본:
- `/jobs/macro-collect-global` (app.py:1016-1109)
- `/jobs/macro-collect-korea` (app.py:1112-1119) — global 위임
- `/jobs/macro-validate-store` (app.py:1122-1171)
- `/jobs/macro-quick` (app.py:1174-...) — global + intraday risk
- `/jobs/council-trigger` (app.py:1688-...)
- `/jobs/council-insight` (app.py:1791-...)

Redis key 스키마는 v2 와 호환 유지 (macro:data:snapshot:{YYYY-MM-DD}). council
핸들러는 Track D `council_logging` 과 조율 중 — 여기선 trigger/insight 껍데기만.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .crawlers.naver_market import fetch_index_data, fetch_investor_flows

logger = logging.getLogger(__name__)

MACRO_SNAPSHOT_KEY_PREFIX = "macro:data:snapshot:"
MACRO_SNAPSHOT_TTL_SEC = 7 * 86400  # v2 와 동일: 일주일 보관
MACRO_VALIDATE_LOOKBACK_DAYS = 7
MACRO_REQUIRED_FIELDS: tuple[str, ...] = ("kospi_index", "kosdaq_index")
MACRO_KOSPI_RANGE = (1000, 10000)
MACRO_KOSDAQ_RANGE = (300, 3000)

_YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_BOK_ECOS_BASE = "https://ecos.bok.or.kr/api/StatisticSearch"


class MacroCollectError(RuntimeError):
    """macro_collect_global 이 필수 입력(KOSPI/KOSDAQ 지수) 을 얻지 못했을 때."""


class MacroValidationError(RuntimeError):
    """macro_validate_store 가 스냅샷을 찾지 못하거나 필수 필드가 빠졌을 때."""


async def macro_validate_store(redis_client: Any) -> dict[str, Any]:
    """v2 `/jobs/macro-validate-store` 포팅 (app.py:1122-1171).

    최근 7 일 이내의 Redis 스냅샷 (`macro:data:snapshot:{YYYY-MM-DD}`) 을 찾아
    필수 필드 존재 + 값 범위 sanity check. v2 가 `JobResult(success=False,...)` 로
    돌려주던 상황은 v3 에선 `MacroValidationError` 로 올린다 (scheduler runner
    가 `last_status=failed` 에 메시지 기록).
    """
    snapshot_data: dict[str, Any] | None = None
    found_key: str | None = None
    today = date.today()
    for days_ago in range(MACRO_VALIDATE_LOOKBACK_DAYS):
        check_date = today - timedelta(days=days_ago)
        key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{check_date.isoformat()}"
        raw = await redis_client.get(key)
        if raw is not None:
            snapshot_data = json.loads(raw)
            found_key = key
            break

    if snapshot_data is None:
        raise MacroValidationError(
            f"No macro snapshot found in last {MACRO_VALIDATE_LOOKBACK_DAYS} days"
        )

    missing = [
        f for f in MACRO_REQUIRED_FIELDS if f not in snapshot_data or snapshot_data[f] is None
    ]
    if missing:
        raise MacroValidationError(
            f"Macro validation failed — missing fields: {', '.join(missing)}"
        )

    warnings: list[str] = []
    kospi = snapshot_data.get("kospi_index", 0)
    if kospi < MACRO_KOSPI_RANGE[0] or kospi > MACRO_KOSPI_RANGE[1]:
        warnings.append(f"KOSPI index unusual: {kospi}")
    kosdaq = snapshot_data.get("kosdaq_index", 0)
    if kosdaq < MACRO_KOSDAQ_RANGE[0] or kosdaq > MACRO_KOSDAQ_RANGE[1]:
        warnings.append(f"KOSDAQ index unusual: {kosdaq}")

    fields_present = len(
        [k for k, v in snapshot_data.items() if v is not None and k != "data_sources"]
    )
    summary = {
        "key": found_key,
        "snapshot_date": snapshot_data.get("snapshot_date"),
        "fields_present": fields_present,
        "warnings": warnings,
    }
    logger.info(
        "macro_validate_store: key=%s fields=%d warnings=%d",
        found_key,
        fields_present,
        len(warnings),
    )
    return summary


async def _fetch_vix(client: httpx.AsyncClient) -> tuple[float | None, str]:
    """Yahoo ^VIX 5 일 종가에서 마지막 유효값 + regime (v2 app.py:923-957)."""
    try:
        resp = await client.get(
            f"{_YAHOO_CHART_BASE}/%5EVIX",
            params={"range": "5d", "interval": "1d"},
            headers=_YAHOO_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        closes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vix: float | None = None
        for v in reversed(closes):
            if v is not None:
                vix = round(float(v), 2)
                break
        if vix is None:
            return None, "unknown"
        if vix < 15:
            regime = "low_vol"
        elif vix < 25:
            regime = "normal"
        elif vix < 35:
            regime = "elevated"
        else:
            regime = "crisis"
        return vix, regime
    except Exception as e:
        logger.warning("VIX fetch failed: %s", e)
        return None, "unknown"


async def _fetch_us_latest(
    client: httpx.AsyncClient, yahoo_ticker: str, label: str
) -> dict[str, float] | None:
    """Yahoo 미국 지표 5 일 창에서 직전/최신 유효 종가 → 변동률 (v2 app.py:960-987)."""
    try:
        resp = await client.get(
            f"{_YAHOO_CHART_BASE}/{yahoo_ticker}",
            params={"range": "5d", "interval": "1d"},
            headers=_YAHOO_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        closes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        valid = [(i, c) for i, c in enumerate(closes) if c is not None]
        if len(valid) < 2:
            return None
        prev_close = valid[-2][1]
        latest_close = valid[-1][1]
        change_pct = round((latest_close - prev_close) / prev_close * 100, 2)
        return {"close": round(latest_close, 2), "change_pct": change_pct}
    except Exception as e:
        logger.warning("%s fetch failed: %s", label, e)
        return None


async def _fetch_usd_krw(client: httpx.AsyncClient, api_key: str) -> float | None:
    """BOK ECOS 통계 730Y001 (원/달러 매매기준율) 최근 10 일 중 최신 (v2 app.py:990-1013)."""
    try:
        end = date.today()
        start = end - timedelta(days=10)
        url = (
            f"{_BOK_ECOS_BASE}/{api_key}/json/kr/1/10/731Y001/D/"
            f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}/0000001"
        )
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        rows = resp.json().get("StatisticSearch", {}).get("row", [])
        if not rows:
            return None
        return float(rows[-1]["DATA_VALUE"].replace(",", ""))
    except Exception as e:
        logger.warning("USD/KRW fetch failed: %s", e)
        return None


async def macro_collect_global(
    redis_client: Any,
    http: httpx.AsyncClient,
    *,
    bok_ecos_api_key: str | None = None,
) -> dict[str, Any]:
    """v2 `/jobs/macro-collect-global` 포팅 (app.py:1016-1109).

    네이버 금융 → KOSPI/KOSDAQ 지수/수급, Yahoo → VIX/SOX/NVDA, BOK ECOS →
    USD/KRW. `macro:data:snapshot:{YYYY-MM-DD}` 에 merge-upsert 저장 (기존
    글로벌 필드 보존, 한국 필드만 갱신). TTL 7일. macro_validate_store 와
    동일 schema.

    kospi/kosdaq 지수 둘 다 실패 시 `MacroCollectError` — scheduler runner 가
    실패로 기록. Yahoo/BOK 는 실패해도 스냅샷은 저장한다 (v2 와 동일).
    """
    snapshot: dict[str, Any] = {}
    trading_date: date | None = None

    for code, prefix in (("KOSPI", "kospi"), ("KOSDAQ", "kosdaq")):
        idx = await fetch_index_data(http, code)
        if idx is None:
            continue
        snapshot[f"{prefix}_index"] = idx.close
        snapshot[f"{prefix}_change_pct"] = idx.change_pct
        if trading_date is None:
            trading_date = idx.traded_at

    if trading_date is None:
        raise MacroCollectError("No trading data available from Naver")

    td_str = trading_date.strftime("%Y%m%d")
    for market, prefix in (("kospi", "kospi"), ("kosdaq", "kosdaq")):
        flows = await fetch_investor_flows(http, market, td_str)
        if flows is None:
            continue
        snapshot[f"{prefix}_foreign_net"] = flows.foreign_net
        if prefix == "kospi":
            snapshot["kospi_institutional_net"] = flows.institutional_net
            snapshot["kospi_retail_net"] = flows.retail_net

    vix, vix_regime = await _fetch_vix(http)
    if vix is not None:
        snapshot["vix"] = vix
        snapshot["vix_regime"] = vix_regime

    sox_data = await _fetch_us_latest(http, "^SOX", "SOX")
    if sox_data:
        snapshot["sox_close"] = sox_data["close"]
        snapshot["sox_change_pct"] = sox_data["change_pct"]

    nvda_data = await _fetch_us_latest(http, "NVDA", "NVDA")
    if nvda_data:
        snapshot["nvda_close"] = nvda_data["close"]
        snapshot["nvda_change_pct"] = nvda_data["change_pct"]

    # Phase 2.13-2 — 아시아 지수 / 환율 / 원자재. Yahoo chart API 공통.
    # 미장(SOX/NVDA/VIX) 은 기존. 여기 추가 지표는 MarketSnapshotFeeder 가 참조.
    for yahoo_ticker, key_prefix in (
        ("^N225", "nikkei"),
        ("^HSI", "hsi"),
        ("JPY=X", "usd_jpy"),
        # crude_oil 필드는 브렌트유(BZ=F)로 적재한다. 한국은 원유 전량 수입국이고
        # 중동·두바이 가격에 연동돼 WTI 보다 브렌트가 수입 충격을 더 잘 반영한다.
        # (2026-06-09 WTI→Brent 전환. 코스피 유가 알파 측정 결과 근거)
        ("BZ=F", "crude_oil"),
        ("GC=F", "gold"),
    ):
        data = await _fetch_us_latest(http, yahoo_ticker, yahoo_ticker)
        if not data:
            continue
        if key_prefix in ("nikkei", "hsi"):
            snapshot[f"{key_prefix}_close"] = data["close"]
            snapshot[f"{key_prefix}_change_pct"] = data["change_pct"]
        else:
            # usd_jpy/crude_oil/gold 은 schema 상 단일 필드 + _change_pct
            snapshot[key_prefix] = data["close"]
            snapshot[f"{key_prefix}_change_pct"] = data["change_pct"]

    usd_krw: float | None = None
    if bok_ecos_api_key:
        usd_krw = await _fetch_usd_krw(http, bok_ecos_api_key)
        if usd_krw is not None:
            snapshot["usd_krw"] = usd_krw

    td_iso = trading_date.isoformat()
    key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{td_iso}"
    existing_raw = await redis_client.get(key)
    if existing_raw:
        existing = json.loads(existing_raw)
        existing.update(snapshot)
        existing["snapshot_time"] = datetime.now().astimezone().isoformat()
        snapshot = existing
    else:
        snapshot["snapshot_date"] = td_iso
        snapshot["snapshot_time"] = datetime.now().astimezone().isoformat()
        snapshot["data_sources"] = ["naver"]

    await redis_client.set(
        key,
        json.dumps(snapshot, ensure_ascii=False, default=str),
        ex=MACRO_SNAPSHOT_TTL_SEC,
    )
    logger.info(
        "macro_collect_global: trading_date=%s KOSPI=%s KOSDAQ=%s VIX=%s USD/KRW=%s",
        td_iso,
        snapshot.get("kospi_index"),
        snapshot.get("kosdaq_index"),
        vix,
        usd_krw,
    )
    return snapshot


async def macro_quick(
    redis_client: Any,
    http: httpx.AsyncClient,
    *,
    bok_ecos_api_key: str | None = None,
) -> dict[str, Any]:
    """v2 `/jobs/macro-quick` 포팅 (app.py:1174-1180).

    v2 는 `macro_collect_global()` 을 호출한 뒤 `_check_intraday_risk()` 로 5 단계
    리스크 레벨(NORMAL/CAUTION/WARNING/DANGER/CRITICAL) 을 계산해 Context 에
    반영한다. Context 모델 (scout/council 공유) 이 v3 에서 아직 포팅되지 않아
    throttle 레이어는 다음 슬라이스로 미룸 — 현재는 snapshot 만 갱신한다.

    v2 스케줄과 동일하게 장중 5분 간격 (`*/5 9-15 * * 1-5`) 으로 돌아야 한다.
    """
    logger.info("macro_quick: collecting snapshot (throttle layer TODO)")
    return await macro_collect_global(redis_client, http, bok_ecos_api_key=bok_ecos_api_key)


async def macro_collect_korea(
    redis_client: Any,
    http: httpx.AsyncClient,
    *,
    bok_ecos_api_key: str | None = None,
) -> dict[str, Any]:
    """v2 `/jobs/macro-collect-korea` 포팅 (app.py:1112-1119).

    v2 는 global 에 위임했다. v3 도 동일한 구현을 재사용 — 별도 크롤링 없음.
    """
    logger.info("macro_collect_korea: delegating to macro_collect_global")
    return await macro_collect_global(redis_client, http, bok_ecos_api_key=bok_ecos_api_key)


__all__ = [
    "MACRO_KOSDAQ_RANGE",
    "MACRO_KOSPI_RANGE",
    "MACRO_REQUIRED_FIELDS",
    "MACRO_SNAPSHOT_KEY_PREFIX",
    "MACRO_SNAPSHOT_TTL_SEC",
    "MACRO_VALIDATE_LOOKBACK_DAYS",
    "MacroCollectError",
    "MacroValidationError",
    "macro_collect_global",
    "macro_collect_korea",
    "macro_quick",
    "macro_validate_store",
]
