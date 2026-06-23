"""/dca 명령 로직 — echo→확인 무장, 가드, 조회, 취소. PG 대신 InMemoryDcaRepo."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.telegram_bot import dca_command
from prime_jennie_runtime.telegram_bot.control import RESPONSE_HELP
from prime_jennie_runtime.user_directed_dca.presets import PRESETS

from .fakes import InMemoryDcaRepo

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 6, 20, 11, 0, tzinfo=KST)

# 텔레그램 HTML parse_mode 가 허용하는 태그만 — 그 외 <...> 는 send 400 으로 무응답.
_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "code", "pre", "a"}


def _html_safe(text: str) -> bool:
    for inner in re.findall(r"<([^>]*)>", text):
        name = inner.lstrip("/").split()[0].lower() if inner.strip() else ""
        if name not in _ALLOWED_TAGS:
            return False
    return True


@pytest.mark.asyncio
async def test_arm_echo_then_confirm_creates_campaigns(fake_redis):
    repo = InMemoryDcaRepo()

    echo = await dca_command.arm(
        repo,
        fake_redis,
        chat_id="42",
        preset_name="production",
        ticker=None,
        confirm=False,
        now=NOW,
    )
    assert "삼성전자" in echo or "005930" in echo
    assert "확인" in echo
    # echo 만으로는 캠페인이 생기지 않는다
    assert not repo.campaigns

    done = await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="production", ticker=None, confirm=True, now=NOW
    )
    assert "무장 완료" in done
    assert len(repo.campaigns) == 2
    assert {c.ticker for c in repo.campaigns.values()} == {"005930", "000660"}
    assert all(c.status == "active" and not c.dry_run for c in repo.campaigns.values())


@pytest.mark.asyncio
async def test_confirm_without_echo_is_rejected(fake_redis):
    repo = InMemoryDcaRepo()
    reply = await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="production", ticker=None, confirm=True, now=NOW
    )
    assert "확인할 무장 내역이 없습니다" in reply
    assert not repo.campaigns


@pytest.mark.asyncio
async def test_dryrun_preset_sets_dry_flag(fake_redis):
    repo = InMemoryDcaRepo()
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="dryrun", ticker=None, confirm=False, now=NOW
    )
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="dryrun", ticker=None, confirm=True, now=NOW
    )
    assert all(c.dry_run for c in repo.campaigns.values())


@pytest.mark.asyncio
async def test_smoke_requires_ticker(fake_redis):
    repo = InMemoryDcaRepo()
    reply = await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="smoke", ticker=None, confirm=False, now=NOW
    )
    assert "종목코드" in reply
    assert not repo.campaigns


@pytest.mark.asyncio
async def test_smoke_with_ticker_arms_one_campaign(fake_redis):
    repo = InMemoryDcaRepo()
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="smoke", ticker="005930", confirm=False, now=NOW
    )
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="smoke", ticker="005930", confirm=True, now=NOW
    )
    assert len(repo.campaigns) == 1
    c = next(iter(repo.campaigns.values()))
    assert c.ticker == "005930" and c.cap_krw == 100_000


@pytest.mark.asyncio
async def test_arm_blocked_when_active_exists(fake_redis):
    repo = InMemoryDcaRepo()
    await dca_command.arm(
        repo,
        fake_redis,
        chat_id="42",
        preset_name="production",
        ticker=None,
        confirm=False,
        now=NOW,
    )
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="production", ticker=None, confirm=True, now=NOW
    )
    # 재무장 시도 — 이미 활성이라 echo 단계에서 거부
    reply = await dca_command.arm(
        repo,
        fake_redis,
        chat_id="42",
        preset_name="production",
        ticker=None,
        confirm=False,
        now=NOW,
    )
    assert "이미 활성" in reply


@pytest.mark.asyncio
async def test_unknown_preset(fake_redis):
    repo = InMemoryDcaRepo()
    reply = await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="bogus", ticker=None, confirm=False, now=NOW
    )
    assert "알 수 없는 preset" in reply


@pytest.mark.asyncio
async def test_cancel_by_ticker_halts(fake_redis):
    repo = InMemoryDcaRepo()
    await dca_command.arm(
        repo,
        fake_redis,
        chat_id="42",
        preset_name="production",
        ticker=None,
        confirm=False,
        now=NOW,
    )
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="production", ticker=None, confirm=True, now=NOW
    )
    reply = await dca_command.cancel(repo, "005930")
    assert "중단" in reply
    halted = [c for c in repo.campaigns.values() if c.ticker == "005930"]
    assert halted[0].status == "halted"
    # 나머지 종목은 그대로 active
    assert any(c.ticker == "000660" and c.status == "active" for c in repo.campaigns.values())


@pytest.mark.asyncio
async def test_cancel_all(fake_redis):
    repo = InMemoryDcaRepo()
    await dca_command.arm(
        repo,
        fake_redis,
        chat_id="42",
        preset_name="production",
        ticker=None,
        confirm=False,
        now=NOW,
    )
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="production", ticker=None, confirm=True, now=NOW
    )
    reply = await dca_command.cancel(repo, "all")
    assert "2개" in reply
    assert all(c.status == "halted" for c in repo.campaigns.values())


async def _arm_production(repo, fake_redis):
    """production 2종목(005930·000660) 무장 — pause/resume 테스트 공통 준비."""
    await dca_command.arm(
        repo,
        fake_redis,
        chat_id="42",
        preset_name="production",
        ticker=None,
        confirm=False,
        now=NOW,
    )
    await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="production", ticker=None, confirm=True, now=NOW
    )


@pytest.mark.asyncio
async def test_pause_then_resume_by_ticker(fake_redis):
    repo = InMemoryDcaRepo()
    await _arm_production(repo, fake_redis)

    paused = await dca_command.pause(repo, "005930")
    assert "일시중지" in paused
    by_ticker = {c.ticker: c for c in repo.campaigns.values()}
    assert by_ticker["005930"].status == "paused"
    # 나머지 종목은 그대로 active — 한 종목만 멈춘다
    assert by_ticker["000660"].status == "active"

    resumed = await dca_command.resume(repo, "005930")
    assert "재개" in resumed
    assert by_ticker["005930"].status == "active"


@pytest.mark.asyncio
async def test_pause_all_then_resume_all(fake_redis):
    repo = InMemoryDcaRepo()
    await _arm_production(repo, fake_redis)

    reply = await dca_command.pause(repo, "all")
    assert "2개" in reply
    assert all(c.status == "paused" for c in repo.campaigns.values())

    reply = await dca_command.resume(repo, "all")
    assert "2개" in reply
    assert all(c.status == "active" for c in repo.campaigns.values())


@pytest.mark.asyncio
async def test_pause_preserves_progress(fake_redis):
    # 일시중지는 보유·진행상태를 보존한다 — slices_done·누적이 그대로여야 한다.
    repo = InMemoryDcaRepo()
    await _arm_production(repo, fake_redis)
    camp = next(c for c in repo.campaigns.values() if c.ticker == "005930")
    camp.slices_done = 2
    camp.cumulative_filled_krw = 23_000_000
    camp.cumulative_filled_qty = 300

    await dca_command.pause(repo, "005930")
    assert camp.status == "paused"
    assert camp.slices_done == 2
    assert camp.cumulative_filled_krw == 23_000_000
    assert camp.cumulative_filled_qty == 300


@pytest.mark.asyncio
async def test_resume_only_targets_paused(fake_redis):
    # active 캠페인을 resume 하려 하면 대상이 없다(이미 돌고 있음). halted 도 마찬가지.
    repo = InMemoryDcaRepo()
    await _arm_production(repo, fake_redis)
    reply = await dca_command.resume(repo, "005930")
    assert "찾지 못했습니다" in reply
    assert all(c.status == "active" for c in repo.campaigns.values())


@pytest.mark.asyncio
async def test_pause_resume_not_found(fake_redis):
    repo = InMemoryDcaRepo()
    assert "없습니다" in await dca_command.pause(repo, "all")
    assert "없습니다" in await dca_command.resume(repo, "all")
    assert "찾지 못했습니다" in await dca_command.pause(repo, "999999")


@pytest.mark.asyncio
async def test_pause_resume_messages_html_safe(fake_redis):
    repo = InMemoryDcaRepo()
    await _arm_production(repo, fake_redis)
    assert _html_safe(await dca_command.pause(repo, "005930"))
    assert _html_safe(await dca_command.resume(repo, "005930"))
    assert _html_safe(dca_command.DCA_USAGE)


def test_status_text_empty_and_populated():
    assert "없습니다" in dca_command.status_text([])


def test_telegram_strings_html_safe():
    # /help 가 <preset> 같은 미지원 태그로 400 무응답 됐던 회귀 방지(2026-06-20).
    assert _html_safe(RESPONSE_HELP)
    assert _html_safe(dca_command.DCA_USAGE)
    assert _html_safe(dca_command.ARM_USAGE)
    legs = list(PRESETS["production"].legs)
    assert _html_safe(dca_command.format_arm_echo(PRESETS["production"], legs))


@pytest.mark.asyncio
async def test_smoke_no_ticker_message_html_safe(fake_redis):
    repo = InMemoryDcaRepo()
    reply = await dca_command.arm(
        repo, fake_redis, chat_id="42", preset_name="smoke", ticker=None, confirm=False, now=NOW
    )
    assert _html_safe(reply)


# ── arm-custom (임의 무장) ─────────────────────────────────────────────


def test_parse_krw_formats():
    assert dca_command.parse_krw("50000000") == 50_000_000
    assert dca_command.parse_krw("50,000,000") == 50_000_000
    assert dca_command.parse_krw("5000만") == 50_000_000
    assert dca_command.parse_krw("1.18억") == 118_000_000
    assert dca_command.parse_krw("0.5억") == 50_000_000
    assert dca_command.parse_krw("abc") is None
    assert dca_command.parse_krw("5.5") is None  # 평문은 정수 원만


def test_parse_hhmm():
    assert dca_command.parse_hhmm("14:00") == "14:00"
    assert dca_command.parse_hhmm("9:5") == "09:05"
    assert dca_command.parse_hhmm("25:00") is None
    assert dca_command.parse_hhmm("1400") is None


def test_build_custom_preset_derives_caps():
    # KODEX200 5천만, 10슬라이스 → 슬라이스당 500만, 건별 3배·일별 4배.
    p = dca_command.build_custom_preset("069500", 50_000_000, 10, "14:00")
    leg = p.legs[0]
    assert leg.ticker == "069500" and leg.cap_krw == 50_000_000
    assert p.total_slices == 10 and p.dry_run is False
    assert p.per_order_cost_cap == 15_000_000  # 5,000,000 × 3
    assert p.per_day_cost_cap == 20_000_000  # 5,000,000 × 4
    assert p.execute_at_kst == "14:00"


@pytest.mark.asyncio
async def test_arm_custom_echo_then_confirm(fake_redis):
    repo = InMemoryDcaRepo()
    echo = await dca_command.arm_custom(
        repo,
        fake_redis,
        chat_id="42",
        ticker="069500",
        cap_krw=50_000_000,
        slices=8,
        execute_at="14:30",
        confirm=False,
        now=NOW,
    )
    assert "069500" in echo and "확인" in echo
    assert _html_safe(echo)
    assert not repo.campaigns  # echo 만으론 무장 안 됨

    done = await dca_command.arm_custom(
        repo,
        fake_redis,
        chat_id="42",
        ticker="069500",
        cap_krw=50_000_000,
        slices=8,
        execute_at="14:30",
        confirm=True,
        now=NOW,
    )
    assert "무장 완료" in done
    camp = repo.campaigns["dca:069500:20260620"]
    assert camp.ticker == "069500"
    assert camp.cap_krw == 50_000_000
    assert camp.total_slices == 8
    assert camp.execute_at_kst == "14:30"
    assert camp.dry_run is False
    assert camp.status == "active"


@pytest.mark.asyncio
async def test_arm_custom_confirm_mismatch_rejected(fake_redis):
    # echo 한 총액과 다른 총액으로 확인하면 거부(2단계 안전).
    repo = InMemoryDcaRepo()
    await dca_command.arm_custom(
        repo,
        fake_redis,
        chat_id="42",
        ticker="069500",
        cap_krw=50_000_000,
        slices=10,
        execute_at="14:00",
        confirm=False,
        now=NOW,
    )
    reply = await dca_command.arm_custom(
        repo,
        fake_redis,
        chat_id="42",
        ticker="069500",
        cap_krw=99_000_000,
        slices=10,
        execute_at="14:00",
        confirm=True,
        now=NOW,
    )
    assert "확인할 무장 내역이 없습니다" in reply
    assert not repo.campaigns


@pytest.mark.asyncio
async def test_arm_custom_validation(fake_redis):
    repo = InMemoryDcaRepo()

    async def _arm(**kw):
        base = dict(
            chat_id="42",
            ticker="069500",
            cap_krw=50_000_000,
            slices=10,
            execute_at="14:00",
            confirm=False,
            now=NOW,
        )
        base.update(kw)
        return await dca_command.arm_custom(repo, fake_redis, **base)

    assert "6자리" in await _arm(ticker="69500")
    assert "너무 작습니다" in await _arm(cap_krw=5_000)
    assert "슬라이스 수는" in await _arm(slices=0)
    assert "슬라이스 수는" in await _arm(slices=99)
    assert "집행 시각은" in await _arm(execute_at="16:00")
    assert not repo.campaigns  # 검증 실패는 무장 안 됨


def test_arm_custom_strings_html_safe():
    assert _html_safe(dca_command.ARM_CUSTOM_USAGE)
    p = dca_command.build_custom_preset("069500", 50_000_000, 10, "14:00")
    assert _html_safe(dca_command.format_custom_echo(p))
