"""KRX 야간선물(H0MFCNT0) 웹소켓 스모크 — REST 야간 미반영의 대체 경로 검증 (2026-07-26).

배경
----
`futures_oi_snapshots` 9거래일 실측에서 **night_open(18:10) 과 night_close(05:05) 가 모든
필드에서 완전히 동일**했다(OI·거래량·가격·베이시스 전부). 설계가 핵심 측정으로 삼은
`close.OI − night_close.OI`("낮에 쌓인 포지션 중 밤에 접힌 양")는 9일 중 7일이 정확히 0,
나머지 이틀도 −3/−30 이라 **구조적으로 죽었다**. REST(`FHMIF10000000`)에 야간장 시장구분
코드가 아예 없다는 것도 실측으로 확인했다(`F` 외 전부 INVALID 또는 빈 output1).

사전 등록된 대체 경로가 웹소켓 `H0MFCNT0` 다. 구독 ACK 는 일요일에도 SUBSCRIBE SUCCESS 로
돌아오지만, **이 프로젝트는 rt_cd=0 + 값 전부 0 에 여러 번 데였다**(KIS 투자자별 선물,
DART 빈 키). ACK 는 "구독이 받아들여졌다"는 뜻일 뿐 "데이터가 흐른다"는 뜻이 아니다.
그래서 실제 야간장 중에 프레임이 오는지, 거기 미결제약정이 실려 오는지를 눈으로 본다.

무엇을 판정하나
--------------
1. **프레임이 오는가** — 안 오면 웹소켓 경로도 죽은 것이고 판정 ① 은 폐기 대상이다.
2. **OI 가 실려 오는가** — H0MFCNT0 필드 배치를 실측해 인덱스를 확정한다(추측 금지).
3. **REST 가 정말 눈이 먼 것인가** — 웹소켓 수신과 같은 순간에 REST 를 찍어 비교한다.
   웹소켓 OI 는 움직이는데 REST 가 그대로면 "REST 미반영"이 증명된다. 둘 다 안 움직이면
   야간장 자체가 조용한 것이므로 결론이 정반대가 된다. 이 구분이 이 스크립트의 핵심이다.

실행 (야간장 18:00~05:00, 평일. 운영 컨테이너에 scripts/ 가 없으므로 stdin 으로 흘린다)
    cat scripts/futures_night_ws_smoke.py | ssh prime-jennie \
      "docker exec -i prime-jennie-runtime-kis-gateway-1 python - --minutes 10"

조회 전용 — 주문·적재 없음.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time

import httpx
import websockets

from prime_jennie_runtime.infra.config import KISConfig
from prime_jennie_runtime.kis_gateway.kis_api import KISApi

WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
TR_NIGHT_EXEC = "H0MFCNT0"  # KRX 야간선물 실시간체결

# 근월·차월을 다 본다 — 롤오버 중화는 주간 수집기와 같은 규율.
DEFAULT_CONTRACTS = ["A01609", "A01612"]


async def _approval_key(cfg: KISConfig) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{cfg.base_url}/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": cfg.app_key,
                "secretkey": cfg.app_secret,
            },
        )
    resp.raise_for_status()
    return resp.json().get("approval_key", "")


async def _rest_snapshot(api: KISApi, contracts: list[str]) -> dict[str, tuple[int, int, float]]:
    """같은 순간의 REST 값 — (OI, 누적거래량, 가격). 웹소켓과 갈라지는지 보는 대조군."""
    out: dict[str, tuple[int, int, float]] = {}
    for code in contracts:
        with contextlib.suppress(Exception):
            q = await api.get_futures_quote(code)
            if q is not None:
                out[code] = (q.open_interest, q.volume, q.price)
    return out


def _describe_frame(raw: str) -> tuple[str, list[str]] | None:
    """KIS 실시간 데이터프레임 → (tr_id, 필드배열). 제어 프레임(JSON)은 None."""
    if raw.startswith("{"):
        return None
    parts = raw.split("|")
    if len(parts) < 4:
        return None
    return parts[1], parts[3].split("^")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0, help="수신 관찰 시간(분)")
    ap.add_argument("--contracts", nargs="*", default=DEFAULT_CONTRACTS)
    ap.add_argument("--show-frames", type=int, default=3, help="원문 출력할 프레임 수")
    args = ap.parse_args()

    cfg = KISConfig()
    api = KISApi(cfg)
    key = await _approval_key(cfg)
    print(f"approval_key: {'OK' if key else 'FAILED'}")
    if not key:
        return

    rest_before = await _rest_snapshot(api, args.contracts)
    print(f"REST 시작값: {rest_before}")

    counts: dict[str, int] = {c: 0 for c in args.contracts}
    shown = 0
    field_len: set[int] = set()
    # 계약별로 처음/마지막 프레임을 들고 있다가 야간 중 변화량을 본다.
    first_frame: dict[str, list[str]] = {}
    last_frame: dict[str, list[str]] = {}

    deadline = time.time() + args.minutes * 60
    async with websockets.connect(WS_URL_REAL, ping_interval=None) as ws:
        for code in args.contracts:
            await ws.send(
                json.dumps(
                    {
                        "header": {
                            "approval_key": key,
                            "custtype": "P",
                            "tr_type": "1",
                            "content-type": "utf-8",
                        },
                        "body": {"input": {"tr_id": TR_NIGHT_EXEC, "tr_key": code}},
                    }
                )
            )
            ack = await asyncio.wait_for(ws.recv(), timeout=10.0)
            with contextlib.suppress(json.JSONDecodeError):
                body = json.loads(ack).get("body", {})
                print(f"  구독 {code}: rt_cd={body.get('rt_cd')} {body.get('msg1')}")

        print(f"\n--- {args.minutes}분 수신 관찰 시작 ---")
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except TimeoutError:
                print("  (30초 무수신)")
                continue

            described = _describe_frame(raw)
            if described is None:
                if '"PINGPONG"' in raw:
                    await ws.pong()
                continue

            tr_id, fields = described
            if tr_id != TR_NIGHT_EXEC or not fields:
                continue

            code = fields[0]
            counts[code] = counts.get(code, 0) + 1
            field_len.add(len(fields))
            first_frame.setdefault(code, fields)
            last_frame[code] = fields

            if shown < args.show_frames:
                shown += 1
                print(f"\n  [원문 프레임 {shown}] tr_key={code} 필드수={len(fields)}")
                for i, v in enumerate(fields):
                    print(f"    [{i:>2}] {v}")

    rest_after = await _rest_snapshot(api, args.contracts)
    await api.close()

    print("\n=== 결과 ===")
    print(f"수신 프레임: {counts}")
    print(f"필드 길이 관측: {sorted(field_len)}")
    for code in args.contracts:
        if code in first_frame and code in last_frame:
            changed = [
                f"[{i}] {a} -> {b}"
                for i, (a, b) in enumerate(zip(first_frame[code], last_frame[code], strict=False))
                if a != b
            ]
            print(f"\n  {code} 관찰창 내 변한 필드 {len(changed)}개:")
            for line in changed:
                print(f"    {line}")
    print(f"\nREST 시작: {rest_before}")
    print(f"REST 종료: {rest_after}")
    print(
        "\n판정: 웹소켓 프레임>0 이고 REST 가 시작=종료면 'REST 미반영, 웹소켓 유효'.\n"
        "      웹소켓 프레임=0 이면 야간 경로 자체가 죽은 것 → 판정 ① 폐기 검토.\n"
        "      OI 로 보이는 필드는 위 '변한 필드' 목록에서 자릿수(6자리)로 식별할 것."
    )


asyncio.run(main())
