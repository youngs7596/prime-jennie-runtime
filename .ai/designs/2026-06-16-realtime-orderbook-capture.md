# 실시간 체결·호가 적재 설계 — 2026-06-16

## 왜 하는가

KIS 실시간 웹소켓으로 들어오는 체결과 호가는 지나가면 과거로 다시 받을 수 없다.
우리가 API로 모으는 데이터의 상당수는 과거 조회 한계(KIS 체결내역 3개월, 분봉 며칠)가
있고, 실시간 틱·호가는 그 한계의 극단이라 안 받으면 영영 없다. 마침 게이트웨이가
체결 틱을 이미 받고 있으면서 Redis 스트림으로 흘리고 버린다. 이걸 호가까지 더해
게이트웨이에서 DB로 적재해, 우리가 실제로 사고파는 종목의 진입~청산 구간 미시 흐름을
남긴다. 용도는 진입 타이밍·체결 품질·슬리피지 분석이고, 아직 분석 형태가 정해지지
않았으니 원본에 가깝게 모아 둔다.

결정 두 가지(2026-06-16, 사용자):
- 체결뿐 아니라 **호가창까지** 받는다.
- 적재 주체는 **게이트웨이**에 둔다.

## 지금 구조 (근거)

- `kis_gateway/streamer.py`: KIS 웹소켓(`H0STCNT0` 체결만) 수신 → `^` 분해 →
  6개 필드(코드·체결가·고가·매도1·매수1·누적량)를 Redis `kis:prices`(maxlen 10k)에
  XADD. 호가 채널(`H0STASP0`)은 구독하지 않는다. DB엔 안 쓴다.
- `kis_gateway/server.py:224`: lifespan 에서 `asyncpg.create_pool` 로 pg_pool 을
  만들어 `PostgresPriceRepo`(KIS 조회 실패 시 폴백)에 연결한다. **게이트웨이는 이미
  DB 풀을 갖고 있다.**
- `fast_loop/gateway_subscriber.py:34`: `KIS_WS_SUBSCRIPTION_LIMIT = 41` —
  KIS 가 연결당 허용하는 실시간 등록 건수. 보유 종목 먼저, 추천시트는 최신순으로
  41까지 채운다.
- 마이그레이션 최신 = `023`. 002 이후는 배포 후 호스트 psql 로 수동 적용.
- `jobs/maintenance.py`: `cleanup_old_data` 가 매일 03시 daily_prices 365일 경과분
  삭제. 보존 정책을 여기에 얹을 수 있다.

## 무엇을 받고 어디 저장하나

종목당 두 채널을 구독한다.
- `H0STCNT0`(체결): 체결가, 그 틱 체결량, 누적거래량, 체결구분(매수/매도), 체결강도,
  최우선 매도·매수 호가.
- `H0STASP0`(호가): 매도·매수 10단계 호가와 각 잔량, 총잔량.

새 테이블 둘(마이그레이션 024):

```sql
-- 체결 틱
CREATE TABLE realtime_ticks (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  VARCHAR(10) NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,   -- KIS 체결시각(체결시각 + 당일)
    price       INTEGER     NOT NULL,
    volume      INTEGER     NOT NULL,   -- 이 틱 체결량
    cum_volume  BIGINT,                 -- 누적거래량
    trade_sign  SMALLINT,               -- 매수/매도 체결구분
    strength    DOUBLE PRECISION,       -- 체결강도
    best_ask    INTEGER,
    best_bid    INTEGER,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_rt_ticks_code_ts ON realtime_ticks (stock_code, ts);

-- 호가 스냅샷
CREATE TABLE realtime_orderbook (
    id           BIGSERIAL PRIMARY KEY,
    stock_code   VARCHAR(10) NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    ask_prices   INTEGER[]   NOT NULL,  -- 매도 1~10
    ask_volumes  INTEGER[]   NOT NULL,
    bid_prices   INTEGER[]   NOT NULL,  -- 매수 1~10
    bid_volumes  INTEGER[]   NOT NULL,
    total_ask    BIGINT,
    total_bid    BIGINT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_rt_ob_code_ts ON realtime_orderbook (stock_code, ts);
```

호가는 10단계를 배열로 둔다. 분석 형태가 정해지지 않아 넓은 40컬럼보다 배열이
유연하고, 필요하면 `unnest` 로 펼친다.

## 적재 방식 — 게이트웨이 안의 버퍼 + 묶음 쓰기

스트리머의 메시지 루프는 빨라야 한다(KIS PINGPONG 응답이 늦으면 끊긴다). 그래서
틱마다 DB 를 직접 치지 않는다. 게이트웨이 프로세스 안에 작은 메모리 버퍼(asyncio 큐)를
두고, 스트리머는 파싱한 레코드를 큐에 넣기만 한다. 별도 비동기 flush 태스크가 1~2초
또는 N행마다 게이트웨이의 pg_pool 로 묶음 INSERT 한다.

- 새 모듈 `kis_gateway/realtime_persist.py`: 버퍼 + flush 루프. lifespan 에서
  pg_pool 과 함께 생성·시작하고 종료 시 남은 버퍼를 비운다.
- 스트리머는 sink(큐) 를 주입받아 파싱 결과를 넣는다. 스트리머 책임은 좁게 유지.
- 장 시간 게이트는 스트리머의 기존 `is_streaming_hours()` 를 그대로 탄다.

Redis 스트림에 소비자 그룹을 하나 더 붙이는 대안도 있으나 택하지 않는다. `kis:prices`
는 maxlen 10k 라 원본 보존에 부적합하고(부하 시 trim 으로 유실), 게이트웨이 안에서
producer·consumer 가 같은 프로세스라 굳이 Redis 를 거칠 이유가 없다. 메모리 버퍼가 더
단순하고 유실 위험이 작다. 게이트웨이 재시작 시 아직 flush 안 한 1~2초치만 날아가는데
시장 원본 데이터엔 감당 가능한 손실이다.

기존 `kis:prices` 발행은 그대로 둔다. fast_loop 모니터링 경로는 건드리지 않는다.

## 41 한도 처리

한 종목에 체결+호가 두 채널을 걸면 등록이 2건 잡힌다. 연결당 41건이 한도이므로
실효 종목 수는 약 20개로 준다. `gateway_subscriber.py` 의 종목 한도를
`floor(41/2)=20` 으로 내리고, 스트리머의 `_send_subscribe` 가 종목마다 두 tr_id 를
보내도록 고친다. 사람-승인 매매라 보유+추천시트가 20을 넘는 일은 드물어 실제 병목은
아니다. 넘으면 기존 로직대로 보유 먼저, 시트 최신순으로 자른다.

## 변경 지점과 줄수 견적

- `migrations/024_realtime_capture.sql` — 테이블 둘 + 인덱스. 약 35줄.
- `kis_gateway/streamer.py` — H0STASP0 구독(두 tr_id 전송), H0STASP0 파싱,
  H0STCNT0 추가 필드 파싱 + sink push. 약 60줄.
- `kis_gateway/realtime_persist.py` (신규) — 버퍼 + 묶음 flush 태스크. 약 90줄.
- `kis_gateway/server.py` — lifespan 에서 버퍼 생성·pg_pool 전달·태스크 시작/정지,
  스트리머에 sink 주입. 약 20줄.
- `fast_loop/gateway_subscriber.py` — 한도 41→20(등록 2건/종목). 약 8줄.
- `jobs/maintenance.py` — 두 테이블 보존 정리 추가. 약 15줄.
- 테스트 — H0STASP0 파싱, 버퍼 묶음 flush, 한도 절반. 약 130줄.

## Pre-flight (착수 전 확인)

1. **KIS 필드 인덱스 매핑.** `H0STCNT0` 의 체결구분·체결강도 위치와 `H0STASP0` 의
   10단계 호가·잔량 위치를 KIS 문서로 확정한다. 지금 스트리머는 체결의 매도1·매수1·
   누적량 인덱스만 안다. paper 웹소켓에 한 종목 걸어 실제 메시지를 떠서 대조하는 스모크
   확인이 가장 확실하다.
2. **틱 양 실측.** 구독 종목 소수 기준 하루 몇 행이 쌓이는지 한 거래일 관측한다.
   이 숫자가 보존 정책(아래)을 정한다. 분봉이 하루 약 8만 행인데, 활발한 20종목의
   틱은 그보다 훨씬 많을 수 있다.

## 보존 정책 (열어 둠)

원본을 버리면 못 되살리지만 틱은 빠르게 큰다. 대상이 소수 종목이라 양은 묶이나,
실측 전엔 단정하지 않는다. `cleanup_old_data` 에 두 테이블 보존 일수를 별도 knob 으로
두고, 기본값은 Pre-flight 실측 후 정한다. 우선은 넉넉히(예: 90일) 두고 양을 보며 조정.

## 미결·리스크

- KIS 필드 인덱스가 문서와 실제가 어긋날 수 있다 → 스모크 확인 필수.
- 묶음 flush 가 밀리면 버퍼가 커진다 → 버퍼 상한 + 초과 시 경고·드롭 가드.
- 적재가 게이트웨이 부하를 키워 KIS 조회 경로에 영향 줄 수 있다 → flush 는 pg_pool
  의 별도 커넥션, 묶음 간격으로 부하 평탄화. 배포 후 monitor 폴 영향 관측.
- PAUSE 중엔 보유·시트가 적어 데이터가 거의 안 쌓인다. 시나리오 B 실매매가 돌기
  시작하면서 의미 있는 양이 들어온다.

## 적용 순서

1. 024 작성 → 배포 후 MS-01 호스트 psql 로 수동 적용(자동 적용 없음).
2. 코드 변경(스트리머·persist·server·subscriber·maintenance) + 테스트.
3. paper 웹소켓 스모크로 필드 매핑·flush 검증.
4. 한 거래일 틱 양 관측 → 보존 일수 확정.
