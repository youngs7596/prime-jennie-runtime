"""/dca 명령 로직 — echo→"확인" 무장, 진행 조회, 취소.

/accept 의 2단계(echo 시점 값 Redis 10분 보존 → 확인) 패턴을 따른다. repo 는 덕 타이핑
(DcaRepository 또는 테스트 in-memory) 이라 PG 없이 단위 테스트가 된다. 실제 슬라이스
집행은 job-worker tick 이 하고, 여기서는 캠페인을 무장(PG 기록)만 한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from prime_jennie_runtime.user_directed_dca.presets import (
    PRESETS,
    Preset,
    PresetLeg,
    build_campaign_rows,
    resolve_legs,
)
from prime_jennie_runtime.user_directed_dca.state import MIN_ORDER_KRW

KST = timezone(timedelta(hours=9))

KEY_DCA_PENDING_PREFIX = "v3:dca:arm_pending:"
DCA_PENDING_TTL_SEC = 600

# 임의 무장(arm-custom) 한계 — 오타·폭주 방어용. 집행 시각은 tick 의 신규주문 컷오프
# (15:20) 안이어야 하고, 개장 직후 변동성 회피로 09:00 이상.
CUSTOM_MIN_CAP_KRW = 10_000
CUSTOM_MAX_SLICES = 60
CUSTOM_MIN_TIME = "09:00"
CUSTOM_MAX_TIME = "15:20"
CUSTOM_DEFAULT_SLICES = 10
CUSTOM_DEFAULT_TIME = "14:00"

DCA_USAGE = (
    "<b>/dca</b> — 사용자 지정 종목 분할매수\n"
    "/dca status — 진행 상황\n"
    "/dca arm preset [종목] — 프리셋 무장 (echo)\n"
    "/dca arm-custom 종목 총액 [슬라이스] [HH:MM] — 임의 무장 (echo)\n"
    "/dca cancel 종목|all — 중단\n\n"
    "preset: production | smoke 종목코드 | dryrun"
)
ARM_USAGE = "사용법: <code>/dca arm production|dryrun|smoke [종목코드]</code>"
ARM_CUSTOM_USAGE = (
    "사용법: <code>/dca arm-custom 종목코드 총액 [슬라이스수] [HH:MM]</code>\n"
    "예: <code>/dca arm-custom 069500 50000000</code> "
    "(KODEX200 5천만, 10슬라이스, 14:00)\n"
    "총액은 50000000 · 5000만 · 0.5억 형식 모두 가능"
)


def _pending_key(chat_id: str) -> str:
    return f"{KEY_DCA_PENDING_PREFIX}{chat_id}"


async def store_pending_arm(
    redis: Any, chat_id: str, *, preset: str, ticker: str, extra: dict[str, Any] | None = None
) -> None:
    payload: dict[str, Any] = {"preset": preset, "ticker": ticker}
    if extra:
        payload.update(extra)
    await redis.set(_pending_key(chat_id), json.dumps(payload), ex=DCA_PENDING_TTL_SEC)


async def load_pending_arm(redis: Any, chat_id: str) -> dict[str, Any] | None:
    raw = await redis.get(_pending_key(chat_id))
    if not raw:
        return None
    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def clear_pending_arm(redis: Any, chat_id: str) -> None:
    await redis.delete(_pending_key(chat_id))


def format_arm_echo(preset: Preset, legs: list[PresetLeg]) -> str:
    """[종목·배분·총액·슬라이스수·간격·집행시각] 되읽기 — 확인 전 마지막 점검."""
    total = sum(leg.cap_krw for leg in legs)
    leg_lines = "\n".join(f"  · {leg.ticker} — {leg.cap_krw:,}원" for leg in legs)
    mode = "모의(실주문 없음)" if preset.dry_run else "실거래"
    ticker_arg = f" {legs[0].ticker}" if preset.needs_ticker else ""
    return (
        f"<b>DCA 무장 — {preset.label}</b>\n"
        f"종목·배분:\n{leg_lines}\n"
        f"총 투입: {total:,}원 ({mode})\n"
        f"분할: 매 영업일 1회 × {preset.total_slices}슬라이스, {preset.execute_at_kst} 시장가\n"
        f"급락 가속: 시가대비 {preset.accel_open_pct:g}% 또는 "
        f"전일종가대비 {preset.accel_prev_pct:g}% 이하\n\n"
        f"실행: <code>/dca arm {preset.name}{ticker_arg} 확인</code> (10분 내)"
    )


def status_text(campaigns: list[Any]) -> str:
    if not campaigns:
        return "활성 DCA 캠페인이 없습니다. <code>/dca arm production</code> 로 무장."
    lines = ["<b>DCA 캠페인</b>"]
    for c in campaigns:
        pct = (c.cumulative_filled_krw / c.cap_krw * 100) if c.cap_krw else 0
        mode = " (dry)" if c.dry_run else ""
        lines.append(
            f"· {c.ticker} [{c.status}]{mode} — 슬라이스 {c.slices_done}/{c.total_slices}, "
            f"{c.cumulative_filled_krw:,}/{c.cap_krw:,}원 ({pct:.0f}%)"
        )
    return "\n".join(lines)


async def arm(
    repo: Any,
    redis: Any,
    *,
    chat_id: str,
    preset_name: str,
    ticker: str | None,
    confirm: bool,
    now: datetime,
) -> str:
    preset = PRESETS.get(preset_name)
    if preset is None:
        return f"알 수 없는 preset: {preset_name} (production | smoke | dryrun)"
    if preset.needs_ticker and ticker is not None and not (ticker.isdigit() and len(ticker) == 6):
        return "smoke 는 6자리 종목코드가 필요합니다. 예: <code>/dca arm smoke 005930</code>"
    try:
        legs = resolve_legs(preset, ticker)
    except ValueError as e:
        return str(e)

    if confirm:
        return await _confirm_arm(repo, redis, chat_id, preset, ticker, now)

    for leg in legs:
        if await repo.count_active_for_ticker(leg.ticker) > 0:
            return (
                f"{leg.ticker} 는 이미 활성 DCA 캠페인이 있습니다. "
                f"먼저 <code>/dca cancel {leg.ticker}</code>."
            )
    await store_pending_arm(redis, chat_id, preset=preset_name, ticker=ticker or "")
    return format_arm_echo(preset, legs)


async def _confirm_arm(
    repo: Any, redis: Any, chat_id: str, preset: Preset, ticker: str | None, now: datetime
) -> str:
    pending = await load_pending_arm(redis, chat_id)
    if (
        pending is None
        or pending.get("preset") != preset.name
        or pending.get("ticker") != (ticker or "")
    ):
        return (
            "확인할 무장 내역이 없습니다 (10분 만료 또는 불일치). 다시 <code>/dca arm ...</code>."
        )
    arm_date = now.astimezone(KST).date()
    rows = build_campaign_rows(preset, arm_date=arm_date, ticker=ticker or None)
    for r in rows:
        if await repo.count_active_for_ticker(r["ticker"]) > 0:
            await clear_pending_arm(redis, chat_id)
            return f"{r['ticker']} 가 이미 활성이라 무장을 취소했습니다."
    inserted = await repo.insert_campaigns(rows)
    await clear_pending_arm(redis, chat_id)
    mode = "모의(dry-run)" if preset.dry_run else "실거래"
    return (
        f"<b>DCA 무장 완료</b> — {inserted}개 캠페인 ({mode}).\n"
        f"매 영업일 {preset.execute_at_kst} 시장가 분할매수를 시작합니다. "
        f"진행은 <code>/dca status</code>."
    )


def parse_krw(raw: str) -> int | None:
    """총액 파싱 — 평문 원, 또는 '5000만'·'1.18억' 같은 접미사. 불량값이면 None."""
    s = raw.strip().replace(",", "").replace("_", "")
    try:
        if s.endswith("억"):
            return int(round(float(s[:-1]) * 100_000_000))
        if s.endswith("만"):
            return int(round(float(s[:-1]) * 10_000))
        return int(s)  # 평문은 정수 원만 (소수·문자 → ValueError)
    except ValueError:
        return None


def parse_hhmm(raw: str) -> str | None:
    """'HH:MM' 파싱·정규화. 형식·범위 불량이면 None."""
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def build_custom_preset(ticker: str, cap_krw: int, slices: int, execute_at: str) -> Preset:
    """임의 무장용 1종목 Preset. 안전캡(건별·일별)은 슬라이스 예산에서 자동 산출 —
    건별 3배·일별 4배. 부분체결 이월(다음 슬라이스가 2배 목표)·가속을 클램프 없이
    흡수하면서, 한 주문이 캠페인의 일부를 넘지 못하게 막는다. cap 으로 클램프."""
    base = cap_krw / slices
    per_order = min(cap_krw, int(round(base * 3)))
    per_day = min(cap_krw, int(round(base * 4)))
    return Preset(
        name="custom",
        label=f"임의 지정 — {ticker}",
        legs=(PresetLeg(ticker, cap_krw),),
        total_slices=slices,
        dry_run=False,
        per_order_cost_cap=per_order,
        per_day_cost_cap=per_day,
        execute_at_kst=execute_at,
    )


def format_custom_echo(preset: Preset) -> str:
    """임의 무장 되읽기 — 종목·총액·분할·안전캡·가속을 확인 전 점검."""
    leg = preset.legs[0]
    base = leg.cap_krw // preset.total_slices
    return (
        f"<b>DCA 무장 (임의) — {leg.ticker}</b>\n"
        f"총 투입: {leg.cap_krw:,}원 (실거래)\n"
        f"분할: 매 영업일 1회 × {preset.total_slices}슬라이스 "
        f"(슬라이스당 ~{base:,}원), {preset.execute_at_kst} 시장가\n"
        f"안전캡: 건별 {preset.per_order_cost_cap:,}원 · 일별 {preset.per_day_cost_cap:,}원\n"
        f"급락 가속: 시가대비 {preset.accel_open_pct:g}% 또는 "
        f"전일종가대비 {preset.accel_prev_pct:g}% 이하\n\n"
        f"실행: <code>/dca arm-custom {leg.ticker} {leg.cap_krw} "
        f"{preset.total_slices} {preset.execute_at_kst} 확인</code> (10분 내)"
    )


async def arm_custom(
    repo: Any,
    redis: Any,
    *,
    chat_id: str,
    ticker: str,
    cap_krw: int,
    slices: int,
    execute_at: str,
    confirm: bool,
    now: datetime,
) -> str:
    """임의 종목·총액 무장 (프리셋 없이). echo→"확인" 2단계는 프리셋과 동일."""
    if not (ticker.isdigit() and len(ticker) == 6):
        return "6자리 종목코드가 필요합니다. 예: <code>/dca arm-custom 069500 50000000</code>"
    if cap_krw < CUSTOM_MIN_CAP_KRW:
        return f"총액이 너무 작습니다 (최소 {CUSTOM_MIN_CAP_KRW:,}원)."
    if not (1 <= slices <= CUSTOM_MAX_SLICES):
        return f"슬라이스 수는 1~{CUSTOM_MAX_SLICES} 입니다."
    if cap_krw / slices < MIN_ORDER_KRW:
        return "슬라이스당 예산이 최소 주문액보다 작습니다. 총액을 늘리거나 슬라이스를 줄이세요."
    if not (CUSTOM_MIN_TIME <= execute_at <= CUSTOM_MAX_TIME):
        return f"집행 시각은 {CUSTOM_MIN_TIME}~{CUSTOM_MAX_TIME} 입니다."

    preset = build_custom_preset(ticker, cap_krw, slices, execute_at)
    if confirm:
        return await _confirm_custom(repo, redis, chat_id, preset, now)

    if await repo.count_active_for_ticker(ticker) > 0:
        return (
            f"{ticker} 는 이미 활성 DCA 캠페인이 있습니다. 먼저 <code>/dca cancel {ticker}</code>."
        )
    await store_pending_arm(
        redis,
        chat_id,
        preset="custom",
        ticker=ticker,
        extra={"cap_krw": cap_krw, "slices": slices, "execute_at": execute_at},
    )
    return format_custom_echo(preset)


async def _confirm_custom(
    repo: Any, redis: Any, chat_id: str, preset: Preset, now: datetime
) -> str:
    leg = preset.legs[0]
    pending = await load_pending_arm(redis, chat_id)
    if (
        pending is None
        or pending.get("preset") != "custom"
        or pending.get("ticker") != leg.ticker
        or int(pending.get("cap_krw", -1)) != leg.cap_krw
        or int(pending.get("slices", -1)) != preset.total_slices
        or pending.get("execute_at") != preset.execute_at_kst
    ):
        return (
            "확인할 무장 내역이 없습니다 (10분 만료 또는 불일치). 다시 "
            "<code>/dca arm-custom ...</code>."
        )
    if await repo.count_active_for_ticker(leg.ticker) > 0:
        await clear_pending_arm(redis, chat_id)
        return f"{leg.ticker} 가 이미 활성이라 무장을 취소했습니다."
    arm_date = now.astimezone(KST).date()
    rows = build_campaign_rows(preset, arm_date=arm_date)
    inserted = await repo.insert_campaigns(rows)
    await clear_pending_arm(redis, chat_id)
    if inserted == 0:
        return f"{leg.ticker} 는 오늘 이미 무장한 적이 있어 새로 만들지 않았습니다."
    return (
        f"<b>DCA 무장 완료</b> — {leg.ticker} {leg.cap_krw:,}원 실거래.\n"
        f"매 영업일 {preset.execute_at_kst} 시장가 {preset.total_slices}슬라이스 분할매수. "
        f"진행은 <code>/dca status</code>."
    )


async def cancel(repo: Any, target: str) -> str:
    if target == "all":
        actives = await repo.fetch_active_campaigns()
        for c in actives:
            await repo.set_status(c.id, "halted")
        return f"{len(actives)}개 캠페인을 중단했습니다 (매도는 하지 않음)."
    # campaign id 또는 종목코드
    camp = await repo.fetch_campaign(target)
    if camp is not None and camp.status == "active":
        await repo.set_status(camp.id, "halted")
        return f"캠페인 {camp.id} 중단."
    actives = [c for c in await repo.fetch_active_campaigns() if c.ticker == target]
    if not actives:
        return f"활성 캠페인을 찾지 못했습니다: {target}"
    for c in actives:
        await repo.set_status(c.id, "halted")
    return f"{target} 캠페인 {len(actives)}개 중단 (매도는 하지 않음)."


__all__ = [
    "ARM_CUSTOM_USAGE",
    "ARM_USAGE",
    "DCA_USAGE",
    "arm",
    "arm_custom",
    "build_custom_preset",
    "cancel",
    "clear_pending_arm",
    "format_arm_echo",
    "format_custom_echo",
    "load_pending_arm",
    "parse_hhmm",
    "parse_krw",
    "status_text",
    "store_pending_arm",
]
