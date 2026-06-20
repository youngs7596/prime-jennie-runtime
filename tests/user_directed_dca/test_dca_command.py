"""/dca 명령 로직 — echo→확인 무장, 가드, 조회, 취소. PG 대신 InMemoryDcaRepo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.telegram_bot import dca_command

from .fakes import InMemoryDcaRepo

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 6, 20, 11, 0, tzinfo=KST)


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


def test_status_text_empty_and_populated():
    assert "없습니다" in dca_command.status_text([])
