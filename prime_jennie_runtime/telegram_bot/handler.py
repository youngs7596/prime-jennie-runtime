"""Command Handler — Telegram 명령 → control.commands stream publish + 응답 문자열.

v2 ``services/telegram/handler.py`` 744줄을 v3 으로 포팅.

설계:
- 제어 명령 (pause/resume/stop/dryrun/liquidate) 은 control.commands stream
  으로 publish — fast/slow loop 의 ControlConsumer 가 적용.
- 조회 명령 (balance/portfolio/price/pnl 등) 은 KIS gateway HTTP + PG 직접
  쿼리. ``pool`` / ``kis_client`` 가 None 이면 degrade 응답.
- 매매 명령 (buy/sell/sellall) 은 manual_buy/manual_sell/manual_sellall
  ControlKind 로 publish.
- **Allowlist fail-safe**: ``config.allowed_chat_ids`` 가 비면 ``is_allowed``
  False 반환 (D4).
- **Rate limit**: chat 당 ``config.command_min_interval_s`` 초 간격.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_CONTROL_COMMANDS,
    TypedStreamPublisher,
)

from .adoption import (
    ADOPT_FIXED_SL_PCT,
    ADOPT_HOLD_BDAYS,
    build_adoption_sheet,
    find_tracked_sheet_for_ticker,
    find_tracked_state_for_ticker,
    format_exit_rule,
    load_sheet_exit_rules,
    parse_recovery_pct,
    persist_adoption_sheet,
    scan_tracked_states,
)
from .control import (
    KEY_FORCED_LIQUIDATION,
    KEY_MANUAL_TRADE_PREFIX,
    KEY_MAX_BUY_COUNT,
    KEY_MUTE_UNTIL,
    KEY_PRICE_ALERTS,
    KEY_WATCHLIST_MANUAL,
    MANUAL_TRADE_DAILY_LIMIT,
    RESPONSE_DRYRUN_USAGE,
    RESPONSE_HELP,
    RESPONSE_LIQUIDATE_USAGE,
    RESPONSE_NOT_ALLOWED,
    RESPONSE_RATE_LIMITED,
    RESPONSE_RESUME_CONFIRM,
    RESPONSE_STOP_CONFIRM,
    RESPONSE_UNKNOWN,
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    ControlCommand,
    ControlKind,
    rate_limit_key,
)
from .stock_resolver import resolve_stock

if TYPE_CHECKING:
    import asyncpg

    from prime_jennie_runtime.fast_loop.kis_client import KisClient

KST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """process_command 반환 — reply 문자열 + publish 여부."""

    reply: str
    published: ControlCommand | None = None


class CommandHandler:
    """Telegram 명령 처리기 — 파싱 + allowlist + rate limit + stream publish."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        config: TelegramConfig,
        now_fn: Callable[[], datetime] | None = None,
        *,
        pool: asyncpg.Pool | None = None,
        kis_client: KisClient | None = None,
    ) -> None:
        self._redis = redis_client
        self._config = config
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._pool = pool
        self._kis = kis_client
        self._publisher: TypedStreamPublisher[ControlCommand] = TypedStreamPublisher(
            redis_client, STREAM_CONTROL_COMMANDS, ControlCommand
        )
        self._handlers: dict[str, _Handler] = {
            "/help": self._handle_help,
            "/status": self._handle_status,
            "/pause": self._handle_pause,
            "/resume": self._handle_resume,
            "/stop": self._handle_stop,
            "/dryrun": self._handle_dryrun,
            "/liquidate": self._handle_liquidate,
            "/mute": self._handle_mute,
            "/unmute": self._handle_unmute,
            "/alert": self._handle_alert,
            "/alerts": self._handle_alerts,
            "/maxbuy": self._handle_maxbuy,
            "/config": self._handle_config,
            "/watchlist": self._handle_watchlist,
            "/watch": self._handle_watch,
            "/unwatch": self._handle_unwatch,
            "/balance": self._handle_balance,
            "/price": self._handle_price,
            "/portfolio": self._handle_portfolio,
            "/pnl": self._handle_pnl,
            "/diagnose": self._handle_diagnose,
            "/report": self._handle_diagnose,  # v2 alias
            "/buy": self._handle_buy,
            "/sell": self._handle_sell,
            "/sellall": self._handle_sellall,
            "/adopt": self._handle_adopt,
        }

    def is_allowed(self, chat_id: str | int) -> bool:
        allowed = self._config.allowed_chat_ids
        if not allowed:  # fail-safe: 빈 allowlist → 거부
            return False
        return str(chat_id) in set(allowed)

    async def process_command(
        self,
        command: str,
        args: str,
        chat_id: str | int,
        username: str = "",
    ) -> CommandResult:
        """명령 라우팅. 알 수 없는 명령이면 unknown reply."""
        if not self.is_allowed(chat_id):
            logger.warning("Rejected chat_id=%s command=%s", chat_id, command)
            return CommandResult(reply=RESPONSE_NOT_ALLOWED)

        if await self._rate_limited(str(chat_id)):
            return CommandResult(reply=RESPONSE_RATE_LIMITED)

        handler = self._handlers.get(command)
        if handler is None:
            return CommandResult(reply=RESPONSE_UNKNOWN)

        try:
            return await handler(args, chat_id=str(chat_id), username=username)
        except Exception:
            logger.exception("command handler crashed: %s", command)
            return CommandResult(reply="명령 실행 실패 (로그 확인)")

    # ----- rate limit -----

    async def _rate_limited(self, chat_id: str) -> bool:
        key = rate_limit_key(chat_id)
        interval = self._config.command_min_interval_s
        if interval <= 0:
            return False
        # SET key 1 NX EX interval — key 존재 시 실패 → rate limited
        result = await self._redis.set(key, b"1", nx=True, ex=interval)
        return not result

    # ----- publish helper -----

    async def _publish(
        self,
        kind: ControlKind,
        chat_id: str,
        *,
        reason: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> ControlCommand:
        cmd = ControlCommand(
            kind=kind,
            issued_at=self._now(),
            issued_by=f"telegram:{chat_id}",
            reason=reason,
            payload=dict(payload or {}),
        )
        await self._publisher.publish(cmd)
        return cmd

    # ----- handlers -----

    async def _handle_help(self, args: str, **kwargs: object) -> CommandResult:
        return CommandResult(reply=RESPONSE_HELP)

    async def _handle_status(self, args: str, **kwargs: object) -> CommandResult:
        stop = await self._redis.get(STATE_KEY_STOP)
        pause = await self._redis.get(STATE_KEY_PAUSE)
        dry = await self._redis.get(STATE_KEY_DRYRUN)
        armed = await self._redis.get(STATE_KEY_LIQUIDATE_ARMED)

        def _fmt(v: bytes | str | None) -> str:
            return "—" if not v else (v.decode() if isinstance(v, bytes) else str(v))

        reply = (
            "<b>Prime Jennie v3 상태</b>\n"
            f"긴급정지: {_on_off(stop)}\n"
            f"일시정지: {_fmt(pause)}\n"
            f"DRY_RUN: {_on_off(dry)}\n"
            f"강제청산 armed: {_on_off(armed)}\n"
            f"시각: {self._now().strftime('%Y-%m-%d %H:%M:%SZ')}"
        )
        return CommandResult(reply=reply)

    async def _handle_pause(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        reason = args.strip() or "manual_pause"
        cmd = await self._publish("pause", chat_id, reason=reason)
        return CommandResult(
            reply=f"진입 일시정지 요청 발행됨 (사유: {reason})",
            published=cmd,
        )

    async def _handle_resume(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        # /stop /sellall 과 동일한 확인 단계. 봇 재시작 시 텔레그램 update 재전달로
        # 과거 /resume 이 맥락 없이 재실행되는 경로의 마지막 방어선이기도 하다.
        if args.strip() != "확인":
            return CommandResult(reply=RESPONSE_RESUME_CONFIRM)
        cmd = await self._publish("resume", chat_id)
        return CommandResult(reply="재개 요청 발행됨", published=cmd)

    async def _handle_stop(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        if args.strip() != "확인":
            return CommandResult(reply=RESPONSE_STOP_CONFIRM)
        cmd = await self._publish("emergency_stop", chat_id, reason="telegram_emergency")
        return CommandResult(
            reply="<b>긴급 정지 요청 발행됨</b>\n재개: <code>/resume 확인</code>",
            published=cmd,
        )

    async def _handle_dryrun(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            return CommandResult(reply=RESPONSE_DRYRUN_USAGE)
        enabled = arg == "on"
        cmd = await self._publish("set_dryrun", chat_id, payload={"enabled": enabled})
        return CommandResult(
            reply=f"DRY_RUN {'ON (시뮬레이션)' if enabled else 'OFF (실거래)'} 요청 발행됨",
            published=cmd,
        )

    async def _handle_liquidate(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        sub_arg = parts[1].strip() if len(parts) > 1 else ""

        if sub == "arm":
            members = await self._redis.smembers(KEY_FORCED_LIQUIDATION)
            if not members:
                return CommandResult(
                    reply="대상 종목이 없습니다. 먼저 <code>/liquidate add</code> 로 등록하세요."
                )
            cmd = await self._publish("liquidate_arm", chat_id)
            return CommandResult(
                reply=f"강제 청산 armed ({len(members)}종목 — 다음 틱에서 매도)",
                published=cmd,
            )
        if sub == "disarm":
            cmd = await self._publish("liquidate_disarm", chat_id)
            return CommandResult(reply="강제 청산 disarm", published=cmd)
        if sub == "status":
            armed = await self._redis.get(STATE_KEY_LIQUIDATE_ARMED)
            members = await self._redis.smembers(KEY_FORCED_LIQUIDATION)
            if not members:
                return CommandResult(reply=f"강제 청산 armed: {_on_off(armed)}\n대상: 없음")
            lines = [
                "<b>강제 청산 상태</b>",
                f"armed: {_on_off(armed)}",
                f"대상 ({len(members)}종목):",
            ]
            for m in sorted(members):
                code = m.decode() if isinstance(m, bytes) else str(m)
                stock = await resolve_stock(self._pool, code)
                name = stock[1] if stock else code
                lines.append(f"  {name}({code})")
            return CommandResult(reply="\n".join(lines))

        if sub == "list":
            members = await self._redis.smembers(KEY_FORCED_LIQUIDATION)
            if not members:
                return CommandResult(reply="강제 청산 대상이 없습니다.")
            lines = [f"<b>강제 청산 대상 ({len(members)}종목)</b>"]
            for m in sorted(members):
                code = m.decode() if isinstance(m, bytes) else str(m)
                stock = await resolve_stock(self._pool, code)
                name = stock[1] if stock else code
                lines.append(f"  {name}({code})")
            return CommandResult(reply="\n".join(lines))

        if sub == "clear":
            cmd = await self._publish("liquidate_clear", chat_id)
            return CommandResult(reply="강제 청산 대상 전체 초기화 + 스위치 OFF", published=cmd)

        if sub in ("add", "remove"):
            if not sub_arg:
                return CommandResult(reply=f"사용법: <code>/liquidate {sub} 종목명|코드</code>")
            stock = await resolve_stock(self._pool, sub_arg)
            if stock is None:
                return CommandResult(reply=f"종목을 찾을 수 없습니다: {sub_arg}")
            code, name = stock
            if sub == "add":
                cmd = await self._publish("liquidate_add", chat_id, payload={"ticker": code})
                return CommandResult(reply=f"강제 청산 대상 추가: {name}({code})", published=cmd)
            cmd = await self._publish("liquidate_remove", chat_id, payload={"ticker": code})
            return CommandResult(reply=f"강제 청산 대상 제거: {name}({code})", published=cmd)

        return CommandResult(reply=RESPONSE_LIQUIDATE_USAGE)

    # ----- 알림 음소거 -----

    async def _handle_mute(self, args: str, **_: object) -> CommandResult:
        try:
            minutes = int(args.strip())
        except (ValueError, TypeError):
            return CommandResult(reply="사용법: <code>/mute 분</code> (예: /mute 30)")
        if minutes <= 0:
            return CommandResult(reply="분은 1 이상의 정수")
        until = int(time.time()) + minutes * 60
        await self._redis.set(KEY_MUTE_UNTIL, str(until).encode(), ex=minutes * 60 + 60)
        return CommandResult(reply=f"알림을 {minutes}분간 음소거합니다.")

    async def _handle_unmute(self, args: str, **_: object) -> CommandResult:
        await self._redis.delete(KEY_MUTE_UNTIL)
        return CommandResult(reply="알림이 재개됩니다.")

    # ----- 가격 알림 -----

    async def _handle_alert(self, args: str, **_: object) -> CommandResult:
        parts = args.strip().split()
        if len(parts) < 2:
            return CommandResult(reply="사용법: <code>/alert 종목 가격</code>")

        stock = await resolve_stock(self._pool, parts[0])
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {parts[0]}")
        try:
            target_price = int(parts[1].replace(",", ""))
        except ValueError:
            return CommandResult(reply="가격은 숫자로 입력하세요.")
        if target_price <= 0:
            return CommandResult(reply="가격은 양수여야 합니다.")

        code, name = stock
        alert = {
            "stock_code": code,
            "stock_name": name,
            "target_price": target_price,
            "created_at": self._now().isoformat(),
        }
        await self._redis.hset(
            KEY_PRICE_ALERTS, f"{code}:{target_price}", json.dumps(alert).encode()
        )
        await self._redis.expire(KEY_PRICE_ALERTS, 7 * 86400)
        return CommandResult(reply=f"가격 알림 설정: {name}({code}) → {target_price:,}원")

    async def _handle_alerts(self, args: str, **_: object) -> CommandResult:
        raw = await self._redis.hgetall(KEY_PRICE_ALERTS)
        if not raw:
            return CommandResult(reply="설정된 알림이 없습니다.")
        lines = ["<b>가격 알림 목록</b>"]
        for _key, val in sorted(raw.items()):
            try:
                data = json.loads(val.decode() if isinstance(val, bytes) else str(val))
                lines.append(
                    f"  {data['stock_name']}({data['stock_code']}) → {data['target_price']:,}원"
                )
            except (ValueError, KeyError):
                continue
        return CommandResult(reply="\n".join(lines))

    # ----- 설정 -----

    async def _handle_maxbuy(self, args: str, **_: object) -> CommandResult:
        try:
            val = int(args.strip())
        except (ValueError, TypeError):
            return CommandResult(reply="사용법: <code>/maxbuy 횟수</code> (0~20)")
        if not 0 <= val <= 20:
            return CommandResult(reply="0~20 사이 값을 입력하세요.")
        await self._redis.set(KEY_MAX_BUY_COUNT, str(val).encode())
        return CommandResult(reply=f"일일 최대 매수: {val}회로 변경")

    async def _handle_config(self, args: str, **_: object) -> CommandResult:
        pause = await self._redis.get(STATE_KEY_PAUSE)
        stop = await self._redis.get(STATE_KEY_STOP)
        dry = await self._redis.get(STATE_KEY_DRYRUN)
        mute_until = await self._redis.get(KEY_MUTE_UNTIL)
        max_buy = await self._redis.get(KEY_MAX_BUY_COUNT)

        mute_str = "OFF"
        if mute_until:
            try:
                until_ts = int(mute_until.decode() if isinstance(mute_until, bytes) else mute_until)
                remaining = until_ts - int(time.time())
                if remaining > 0:
                    mute_str = f"{remaining // 60}분 남음"
            except ValueError:
                pass

        max_buy_str = (
            (max_buy.decode() if isinstance(max_buy, bytes) else str(max_buy))
            if max_buy
            else "기본값"
        )

        reply = (
            "<b>현재 설정</b>\n"
            f"긴급정지: {_on_off(stop)}\n"
            f"일시정지: {pause.decode() if isinstance(pause, bytes) else (pause or 'OFF')}\n"
            f"DRY_RUN: {_on_off(dry)}\n"
            f"알림 음소거: {mute_str}\n"
            f"일일 최대 매수: {max_buy_str}회"
        )
        return CommandResult(reply=reply)

    # ----- 워치리스트 -----

    async def _handle_watchlist(self, args: str, **_: object) -> CommandResult:
        raw = await self._redis.hgetall(KEY_WATCHLIST_MANUAL)
        if not raw:
            return CommandResult(reply="워치리스트가 비어있습니다.")
        lines = [f"<b>워치리스트</b> ({len(raw)}종목)"]
        for code_b, name_b in sorted(raw.items()):
            code = code_b.decode() if isinstance(code_b, bytes) else str(code_b)
            name = name_b.decode() if isinstance(name_b, bytes) else str(name_b)
            lines.append(f"  {name}({code})")
        return CommandResult(reply="\n".join(lines))

    async def _handle_watch(self, args: str, **_: object) -> CommandResult:
        if not args.strip():
            return CommandResult(reply="사용법: <code>/watch 종목명|코드</code>")
        stock = await resolve_stock(self._pool, args.strip())
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {args.strip()}")
        code, name = stock
        await self._redis.hset(KEY_WATCHLIST_MANUAL, code.encode(), name.encode())
        return CommandResult(reply=f"워치리스트에 추가: {name}({code})")

    async def _handle_unwatch(self, args: str, **_: object) -> CommandResult:
        if not args.strip():
            return CommandResult(reply="사용법: <code>/unwatch 종목명|코드</code>")
        stock = await resolve_stock(self._pool, args.strip())
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {args.strip()}")
        code, name = stock
        removed = await self._redis.hdel(KEY_WATCHLIST_MANUAL, code.encode())
        if not removed:
            return CommandResult(reply=f"워치리스트에 없습니다: {name}({code})")
        return CommandResult(reply=f"워치리스트에서 제거: {name}({code})")

    # ----- KIS gateway proxy -----

    async def _handle_balance(self, args: str, **_: object) -> CommandResult:
        # monitor 발행 잔고 스냅샷 우선 — 원장 동시 조회 충돌 방지 (2026-06-03 학습).
        # 스냅샷이 없을 때만 KIS 클라이언트 (gateway HTTP, 잔고 캐시가 보호) fallback.
        from prime_jennie_runtime.infra.balance_snapshot import read_balance_snapshot

        snap = await read_balance_snapshot(self._redis)
        if snap is not None and snap.get("cash_balance") is not None:
            return CommandResult(reply=f"현금 잔고: <b>{int(snap['cash_balance']):,}원</b>")

        if self._kis is None:
            return CommandResult(reply="KIS 클라이언트 미설정 — /balance 사용 불가")
        try:
            cash = await self._kis.get_cash()
        except Exception as e:
            logger.warning("get_cash failed: %s", e)
            return CommandResult(reply=f"잔고 조회 실패: {e}")
        return CommandResult(reply=f"현금 잔고: <b>{cash:,}원</b>")

    async def _handle_price(self, args: str, **_: object) -> CommandResult:
        if self._kis is None:
            return CommandResult(reply="KIS 클라이언트 미설정 — /price 사용 불가")
        if not args.strip():
            return CommandResult(reply="사용법: <code>/price 종목명|코드</code>")
        stock = await resolve_stock(self._pool, args.strip())
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {args.strip()}")
        code, name = stock
        try:
            snap = await self._kis.get_snapshot(code)
        except Exception as e:
            logger.warning("get_snapshot failed code=%s: %s", code, e)
            return CommandResult(reply=f"가격 조회 실패: {e}")
        return CommandResult(
            reply=(
                f"<b>{name}</b> ({code})\n"
                f"현재가: {snap.price:,}원\n"
                f"시가: {snap.open_price:,}원\n"
                f"등락: {snap.change_pct:+.2f}%\n"
                f"고가: {snap.high_price:,}원\n"
                f"저가: {snap.low_price:,}원"
            )
        )

    async def _handle_portfolio(self, args: str, **_: object) -> CommandResult:
        if self._kis is None:
            return CommandResult(reply="KIS 클라이언트 미설정 — /portfolio 사용 불가")
        try:
            state = await self._kis.get_balance()
        except Exception as e:
            logger.warning("get_balance failed: %s", e)
            return CommandResult(reply=f"포트폴리오 조회 실패: {e}")
        if not state.positions:
            return CommandResult(reply="보유 종목이 없습니다.")
        lines = [f"<b>보유 포트폴리오</b> ({state.position_count}종목)"]
        for p in state.positions:
            lines.append(
                f"  {p.stock_name}({p.stock_code}) "
                f"{p.quantity}주 평균 {p.average_buy_price:,}원"
                + (f" ({p.profit_pct:+.1f}%)" if p.profit_pct is not None else "")
            )
        lines.append(f"\n현금: {state.cash_balance:,}원 / 총자산: {state.total_asset:,}원")
        return CommandResult(reply="\n".join(lines))

    # ----- PG read -----

    async def _handle_pnl(self, args: str, **_: object) -> CommandResult:
        if self._pool is None:
            return CommandResult(reply="DB 미주입 — /pnl 사용 불가")
        # KST 오늘 자정 (00:00) 이후 outcomes
        today_kst = self._now().astimezone(KST).date()
        start = datetime(today_kst.year, today_kst.month, today_kst.day, tzinfo=KST)
        try:
            rows = await self._pool.fetch(
                "SELECT exit_reason, pnl_pct, pnl_krw FROM outcomes "
                "WHERE closed_at >= $1 ORDER BY closed_at",
                start,
            )
        except Exception as e:
            logger.warning("/pnl query failed: %s", e)
            return CommandResult(reply=f"PnL 조회 실패: {e}")
        if not rows:
            return CommandResult(reply="오늘 마감된 매매가 없습니다.")
        total_krw = sum(float(r["pnl_krw"] or 0) for r in rows)
        avg_pct = sum(float(r["pnl_pct"] or 0) for r in rows) / len(rows)
        wins = sum(1 for r in rows if (r["pnl_pct"] or 0) > 0)
        lines = [
            "<b>오늘 마감 손익</b>",
            f"건수: {len(rows)} (승 {wins} / 패 {len(rows) - wins})",
            f"평균 수익률: {avg_pct:+.2f}%",
            f"누적 손익: {total_krw:+,.0f}원",
        ]
        return CommandResult(reply="\n".join(lines))

    # ----- 수동 매매 -----

    async def _check_manual_trade_limit(self, chat_id: str) -> bool:
        """일일 수동매매 한도 확인 + 카운터 증가. True 면 허용."""
        today = self._now().astimezone(KST).date().isoformat()
        key = f"{KEY_MANUAL_TRADE_PREFIX}{today}:{chat_id}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 86400)
            # 한도 초과 — 증가분은 자연스럽게 다음 날 만료
            return count <= MANUAL_TRADE_DAILY_LIMIT
        except Exception:
            logger.exception("manual_trade_limit check failed")
            return True  # fail-open (다른 가드가 잡음)

    async def _is_stopped(self) -> bool:
        return bool(await self._redis.get(STATE_KEY_STOP))

    async def _is_paused(self) -> bool:
        return bool(await self._redis.get(STATE_KEY_PAUSE))

    async def _handle_buy(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        if await self._is_stopped():
            return CommandResult(reply="긴급정지 상태입니다. 재개: <code>/resume 확인</code>")
        if await self._is_paused():
            return CommandResult(
                reply="일시정지 상태입니다 (진입 차단). 재개: <code>/resume 확인</code>"
            )
        if not await self._check_manual_trade_limit(chat_id):
            return CommandResult(reply="일일 수동매매 한도에 도달했습니다.")
        parts = args.strip().split()
        if not parts:
            return CommandResult(reply="사용법: <code>/buy 종목 [수량]</code>")
        stock = await resolve_stock(self._pool, parts[0])
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {parts[0]}")
        code, name = stock

        qty: int | None = None
        if len(parts) > 1 and parts[1].isdigit():
            qty = int(parts[1])
        elif self._kis is not None:
            try:
                cash = await self._kis.get_cash()
                snap = await self._kis.get_snapshot(code)
                if snap.price > 0:
                    qty = int((cash * 0.20) / snap.price)
            except Exception:
                logger.exception("auto-quantity calc failed code=%s", code)
        if not qty or qty <= 0:
            return CommandResult(reply="수량을 계산할 수 없습니다. 직접 입력하세요.")

        cmd = await self._publish("manual_buy", chat_id, payload={"ticker": code, "quantity": qty})
        return CommandResult(reply=f"매수 요청 발행: {name}({code}) {qty}주", published=cmd)

    async def _handle_sell(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        if await self._is_stopped():
            return CommandResult(reply="긴급정지 상태입니다. 재개: <code>/resume 확인</code>")
        # PAUSE 는 청산 허용 (docstring "진입만 중단")
        if not await self._check_manual_trade_limit(chat_id):
            return CommandResult(reply="일일 수동매매 한도에 도달했습니다.")
        parts = args.strip().split()
        if not parts:
            return CommandResult(reply="사용법: <code>/sell 종목 [수량|전량]</code>")
        stock = await resolve_stock(self._pool, parts[0])
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {parts[0]}")
        code, name = stock

        qty_str = parts[1] if len(parts) > 1 else "전량"
        is_full = qty_str in ("전량", "all", "ALL")

        qty = 0
        if is_full:
            if self._kis is None:
                return CommandResult(reply="KIS 미주입 — 전량 매도는 직접 수량 입력")
            try:
                state = await self._kis.get_balance()
                position = next((p for p in state.positions if p.stock_code == code), None)
            except Exception:
                logger.exception("get_balance failed for /sell")
                return CommandResult(reply="잔고 조회 실패")
            if position is None:
                return CommandResult(reply=f"보유하고 있지 않습니다: {name}({code})")
            qty = position.quantity
        else:
            qty = int(qty_str) if qty_str.isdigit() else 0

        if qty <= 0:
            return CommandResult(reply="매도 수량이 올바르지 않습니다.")

        cmd = await self._publish("manual_sell", chat_id, payload={"ticker": code, "quantity": qty})
        label = "전량" if is_full else f"{qty}주"
        return CommandResult(reply=f"매도 요청 발행: {name}({code}) {label}", published=cmd)

    async def _handle_sellall(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        if await self._is_stopped():
            return CommandResult(reply="긴급정지 상태입니다. 재개: <code>/resume 확인</code>")
        if args.strip() != "확인":
            return CommandResult(reply="전체 청산: <code>/sellall 확인</code> 으로 실행")
        cmd = await self._publish("manual_sellall", chat_id, reason="telegram_sellall")
        return CommandResult(
            reply="<b>전체 청산 요청 발행</b> (실행은 fast-loop 에서)", published=cmd
        )

    async def _handle_adopt(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        """실보유 종목 v3 편입 + 조건부 매도(recovery_exit) 등록 / 조회 / 취소.

        2단계 확인: ``/adopt 종목 회복선%`` 는 해석 echo 만, 끝에 ``확인`` 을
        붙여야 실행 — 사람-승인 매매 설계 §2 시나리오 A 의 결정론 원형.
        ``list`` / ``cancel`` 서브명령은 설계 §4 의 standing order 가시성 보장.
        """
        parts = args.strip().split()
        sub = parts[0].lower() if parts else ""
        if sub == "list":
            return await self._handle_adopt_list()
        if sub == "cancel":
            return await self._handle_adopt_cancel(parts[1:], chat_id)
        confirm = bool(parts) and parts[-1] == "확인"
        if confirm:
            parts = parts[:-1]
        if len(parts) != 2:
            return CommandResult(
                reply=(
                    "사용법: <code>/adopt 종목 회복선%</code>\n"
                    "예: <code>/adopt 서진시스템 -1</code> — 손익률 -1% 이상 회복 시 전량 매도\n"
                    "해석을 본 뒤 끝에 <code>확인</code> 을 붙여 등록합니다.\n"
                    "조회: <code>/adopt list</code> · 취소: <code>/adopt cancel 종목</code>"
                )
            )
        term, pct_raw = parts
        pct = parse_recovery_pct(pct_raw)
        if pct is None:
            return CommandResult(reply="회복선은 -10 ~ 0 사이의 % 값이어야 합니다. 예: -1, -2.5, 0")
        if self._pool is None or self._kis is None:
            return CommandResult(reply="DB/KIS 미연결 — /adopt 를 처리할 수 없습니다.")

        stock = await resolve_stock(self._pool, term)
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {term}")
        code, name = stock

        existing = await find_tracked_sheet_for_ticker(self._redis, code)
        if existing:
            return CommandResult(
                reply=(
                    f"{name}({code}) 는 이미 v3 가 추적 중입니다 (시트 {existing}). 중복 편입 불가."
                )
            )

        try:
            portfolio = await self._kis.get_balance()
        except Exception:
            logger.exception("/adopt get_balance failed")
            return CommandResult(reply="잔고 조회 실패 — 잠시 후 다시 시도하세요.")
        position = next((p for p in portfolio.positions if p.stock_code == code), None)
        if position is None:
            return CommandResult(reply=f"보유하고 있지 않습니다: {name}({code})")

        trigger_price = int(position.average_buy_price * (1 + pct))
        summary = (
            f"<b>{name}({code}) {position.quantity}주 — v3 편입 + 조건부 매도</b>\n"
            f"매입가 {position.average_buy_price:,}원 기준 손익률 {pct * 100:.1f}% 이상 회복 시 "
            f"전량 시장가 매도 (발동가 약 {trigger_price:,}원)\n"
            f"안전장치: 매입가 대비 -{int(ADOPT_FIXED_SL_PCT * 100)}% 손절 "
            f"+ {ADOPT_HOLD_BDAYS}거래일 시간 종료 포함"
        )
        if not confirm:
            return CommandResult(
                reply=f"{summary}\n\n등록: <code>/adopt {term} {pct_raw} 확인</code>"
            )

        sheet = build_adoption_sheet(
            ticker=code,
            avg_price=position.average_buy_price,
            quantity=position.quantity,
            recovery_pct=pct,
            now=self._now(),
            issued_by=f"telegram:{chat_id}",
            hypothesis=f"사용자 조건부 매도: 손익률 {pct * 100:.1f}% 이상 회복 시 전량 매도",
        )
        try:
            await persist_adoption_sheet(self._pool, sheet)
        except Exception:
            logger.exception("/adopt sheet persist failed sheet=%s", sheet.sheet_id)
            return CommandResult(reply="시트 영속 실패 — 등록 중단 (로그 확인)")
        cmd = await self._publish(
            "adopt_position",
            chat_id,
            reason="manual_adopt",
            payload={
                "sheet_id": sheet.sheet_id,
                "ticker": code,
                "entry_price": float(position.average_buy_price),
                "quantity": int(position.quantity),
                "entered_at": sheet.generated_at.isoformat(),
            },
        )
        stopped = await self._is_stopped()
        stop_note = (
            "\n주의: 현재 STOP 상태 — 발동해도 주문이 나가지 않습니다. "
            "해제: <code>/resume 확인</code>"
            if stopped
            else ""
        )
        return CommandResult(
            reply=f"등록됨 — {summary}\n시트: <code>{sheet.sheet_id}</code>{stop_note}",
            published=cmd,
        )

    async def _handle_adopt_list(self) -> CommandResult:
        """등록된 조건부 주문 전체 조회 — tracker 가 추적 중인 시트 + exit 룰.

        tracker 의 Redis 영속 state 가 "지금 fast-loop 이 평가하는 것" 의 정본,
        룰 내용은 position_sheets 의 sheet_json 에서 보강. DB 미주입/조회 실패
        시에도 종목·수량은 보여준다 (가시성이 우선).
        """
        states = await scan_tracked_states(self._redis)
        if not states:
            return CommandResult(reply="등록된 조건부 주문이 없습니다.")
        rules_by_sheet: dict[str, list[dict]] = {}
        if self._pool is not None:
            try:
                rules_by_sheet = await load_sheet_exit_rules(
                    self._pool, [str(s["sheet_id"]) for s in states]
                )
            except Exception:
                logger.exception("/adopt list sheet_json load failed")
        lines = [f"<b>등록된 조건부 주문 ({len(states)}건)</b>"]
        for st in states:
            sheet_id = str(st.get("sheet_id", ""))
            ticker = str(st.get("ticker", ""))
            entry = float(st.get("entry_price") or 0)
            qty = int(st.get("quantity") or 0)
            stock = await resolve_stock(self._pool, ticker)
            name = stock[1] if stock else ticker
            lines.append(f"\n<b>{name}({ticker})</b> {qty}주 · 매입 {int(entry):,}원")
            rules = rules_by_sheet.get(sheet_id)
            if rules:
                lines.extend(f"  · {format_exit_rule(r, entry)}" for r in rules)
            else:
                lines.append("  · 룰 정보 없음 (시트 조회 실패)")
            lines.append(f"  시트 <code>{sheet_id}</code>")
        lines.append("\n취소: <code>/adopt cancel 종목</code>")
        return CommandResult(reply="\n".join(lines))

    async def _handle_adopt_cancel(self, parts: list[str], chat_id: str) -> CommandResult:
        """조건부 주문 취소 — tracker 추적 종료 (등록의 역방향, 2단계 확인).

        실제 제거는 fast-loop 의 ControlCommandConsumer 가 in-memory tracker 에
        적용 — telegram-bot 이 Redis 키만 지우면 fast-loop 메모리에 남아 다음
        persist 때 되살아난다 (adopt 와 같은 이유로 control 명령 경유).
        """
        confirm = bool(parts) and parts[-1] == "확인"
        if confirm:
            parts = parts[:-1]
        if len(parts) != 1:
            return CommandResult(
                reply=(
                    "사용법: <code>/adopt cancel 종목</code>\n"
                    "해석을 본 뒤 끝에 <code>확인</code> 을 붙여 취소합니다."
                )
            )
        term = parts[0]
        stock = await resolve_stock(self._pool, term)
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {term}")
        code, name = stock
        state = await find_tracked_state_for_ticker(self._redis, code)
        if state is None:
            return CommandResult(
                reply=(
                    f"{name}({code}) 는 v3 가 추적 중이 아닙니다. 조회: <code>/adopt list</code>"
                )
            )
        sheet_id = str(state["sheet_id"])
        entry = float(state.get("entry_price") or 0)
        qty = int(state.get("quantity") or 0)
        rule_lines: list[str] = []
        if self._pool is not None:
            try:
                rules_by_sheet = await load_sheet_exit_rules(self._pool, [sheet_id])
                rule_lines = [
                    f"  · {format_exit_rule(r, entry)}" for r in rules_by_sheet.get(sheet_id, [])
                ]
            except Exception:
                logger.exception("/adopt cancel sheet_json load failed sheet=%s", sheet_id)
        summary = (
            f"<b>{name}({code}) {qty}주 — 조건부 주문 취소</b>\n"
            + ("해제되는 룰:\n" + "\n".join(rule_lines) + "\n" if rule_lines else "")
            + "취소하면 v3 청산 관리에서 빠집니다 — 조건부 매도·손절·시간 종료가 "
            "모두 해제되고, 이후 매도는 <code>/sell</code> 수동뿐입니다."
        )
        if not confirm:
            return CommandResult(
                reply=f"{summary}\n\n취소 실행: <code>/adopt cancel {term} 확인</code>"
            )
        cmd = await self._publish(
            "untrack_position",
            chat_id,
            reason="manual_adopt_cancel",
            payload={"sheet_id": sheet_id, "ticker": code},
        )
        return CommandResult(
            reply=(
                f"취소 발행됨 — {name}({code}) 시트 <code>{sheet_id}</code>\n"
                "반영 확인: 잠시 후 <code>/adopt list</code>"
            ),
            published=cmd,
        )

    async def _handle_diagnose(self, args: str, **_: object) -> CommandResult:
        checks: list[str] = []
        try:
            await self._redis.ping()
            checks.append("Redis: OK")
        except Exception:
            checks.append("Redis: FAIL")

        if self._pool is None:
            checks.append("DB: 미주입")
        else:
            try:
                await self._pool.fetchval("SELECT 1")
                checks.append("DB: OK")
            except Exception:
                checks.append("DB: FAIL")

        if self._kis is None:
            checks.append("KIS Gateway: 미주입")
        else:
            try:
                await self._kis.get_cash()
                checks.append("KIS Gateway: OK")
            except Exception:
                checks.append("KIS Gateway: FAIL")

        stop = await self._redis.get(STATE_KEY_STOP)
        pause = await self._redis.get(STATE_KEY_PAUSE)
        return CommandResult(
            reply=(
                "<b>시스템 진단</b>\n"
                + "\n".join(checks)
                + f"\n\n긴급정지: {_on_off(stop)}\n일시정지: {_on_off(pause)}"
            )
        )


def _on_off(v: bytes | str | None) -> str:
    if not v:
        return "OFF"
    return "ON"


# 핸들러 시그니처: (args, chat_id, username) → CommandResult
_Handler = Callable[..., Awaitable[CommandResult]]


def parse_command(text: str) -> tuple[str, str] | None:
    """``/command rest of args`` 를 (command, args) 튜플로.

    봇 이름 mention (``/status@MyBot``) 도 처리. 슬래시로 시작 안 하면 None.
    """
    text = text.strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    return cmd, rest.strip()


__all__ = ["CommandHandler", "CommandResult", "parse_command"]
