"""KIS WebSocket 복구 probe — 실계좌 키로 WS 연결/구독/해제 1회 테스트.

배경: v2 초기에 WebSocket 을 재접속+add-only 패턴으로 abuse 판정 → 차단 → polling 전환.
시간이 지나 차단이 풀렸는지 확인하기 위한 최소 탐침.

흐름:
  1. POST /oauth2/Approval 로 approval_key 발급
  2. ws://ops.koreainvestment.com:21000 연결
  3. H0STCNT0 005930 구독 (tr_type=1)
  4. N 초 동안 수신 프레임 카운트
  5. 구독 해제 (tr_type=2)
  6. 정상 close

환경변수:
  KIS_APP_KEY / KIS_APP_SECRET (실계좌)
  KIS_BASE_URL (기본: 실전)
  KIS_IS_PAPER=false 가정 (paper WS 는 별도 포트 31000)

실행:
  uv run python -m scripts.kis_ws_probe --duration 8 --ticker 005930
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

import httpx
import websockets

logger = logging.getLogger("kis_ws_probe")

WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"


async def get_approval_key(base_url: str, app_key: str, app_secret: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": app_key,
                "secretkey": app_secret,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Approval HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    key = data.get("approval_key")
    if not key:
        raise RuntimeError(f"No approval_key in response: {data}")
    return key


def _build_subscribe_msg(approval_key: str, ticker: str, tr_type: str) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": tr_type,
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "H0STCNT0",
                    "tr_key": ticker,
                }
            },
        }
    )


async def probe(
    base_url: str, ws_url: str, app_key: str, app_secret: str, ticker: str, duration: float
) -> int:
    print(f"[1/5] approval_key 요청 ({base_url}/oauth2/Approval)")
    t0 = time.time()
    try:
        approval_key = await get_approval_key(base_url, app_key, app_secret)
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return 1
    print(f"  OK: approval_key={approval_key[:12]}... ({time.time() - t0:.2f}s)")

    print(f"[2/5] WS connect {ws_url}")
    try:
        async with websockets.connect(ws_url, open_timeout=10) as ws:
            print(f"  OK: connected ({time.time() - t0:.2f}s)")

            print(f"[3/5] subscribe H0STCNT0 {ticker}")
            await ws.send(_build_subscribe_msg(approval_key, ticker, "1"))

            frame_count = 0
            exec_count = 0
            subscribe_ack = None
            deadline = time.time() + duration
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.2, deadline - time.time())
                    )
                except TimeoutError:
                    break
                frame_count += 1
                if msg.startswith("{"):
                    try:
                        payload = json.loads(msg)
                    except json.JSONDecodeError:
                        payload = None
                    if payload:
                        body = payload.get("body") or {}
                        rt = body.get("rt_cd")
                        msg1 = body.get("msg1") or body.get("msg") or ""
                        if subscribe_ack is None:
                            subscribe_ack = (rt, msg1)
                            print(f"  subscribe ack: rt_cd={rt} msg={msg1!r}")
                else:
                    # 실시간 체결 프레임 (파이프 구분 텍스트)
                    exec_count += 1

            print(f"[4/5] received: frames={frame_count} exec_frames={exec_count} in {duration}s")

            print("[5/5] unsubscribe + close")
            try:
                await ws.send(_build_subscribe_msg(approval_key, ticker, "2"))
            except Exception as e:
                print(f"  WARN unsubscribe send: {e}")
    except websockets.exceptions.WebSocketException as e:
        print(f"  FAIL WebSocket: {type(e).__name__}: {e}")
        return 2
    except Exception as e:
        print(f"  FAIL unknown: {type(e).__name__}: {e}")
        return 3

    print("=== RESULT ===")
    if subscribe_ack and subscribe_ack[0] == "0":
        print(f"PASS — WebSocket subscribe acknowledged. Ban 해제로 보임. frames={frame_count}")
        return 0
    elif subscribe_ack:
        print(f"FAIL — subscribe returned rt_cd={subscribe_ack[0]} msg={subscribe_ack[1]!r}")
        return 4
    elif frame_count > 0:
        print(f"MAYBE — {frame_count} frames but no subscribe ack detected (장외 시간?)")
        return 0
    else:
        print("FAIL — no frames received (ban 또는 장외)")
        return 5


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", default="005930")
    p.add_argument("--duration", type=float, default=8.0)
    p.add_argument(
        "--paper-ws", action="store_true", help="paper WS 포트 31000 사용 (기본: real 21000)"
    )
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]
    base_url = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
    ws_url = WS_URL_PAPER if args.paper_ws else WS_URL_REAL

    return asyncio.run(probe(base_url, ws_url, app_key, app_secret, args.ticker, args.duration))


if __name__ == "__main__":
    sys.exit(main())
