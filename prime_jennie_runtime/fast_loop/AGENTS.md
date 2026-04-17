# `fast_loop/` — 빠른 루프 (Executor)

Track C 소유. **절대 원칙: LLM 호출 금지.**

## 책임

- Redis Stream `v3:position_sheets` 소비
- PositionSheet의 exit rules 실시간 평가 (틱마다 first_match)
- KIS Gateway HTTP 클라이언트를 통한 주문 실행
- Intraday Risk Throttle (v2 포팅)

## v2 포팅 대상

| 컴포넌트 | v2 원본 경로 |
|---|---|
| Executor 프레임 | `prime_jennie/services/buyer/executor.py`, `seller/executor.py` |
| Risk Throttle | `prime_jennie/services/jobs/app.py` `_check_intraday_risk()` |
| Position sizing | `prime_jennie/services/buyer/position_sizing.py` |

## 설계 원칙

- 결정론적 코드만. 모든 판단은 PositionSheet의 exit.rules[]에 이미 정의됨
- KIS 복잡도는 `kis_gateway/`로 분리. 여기서는 얇은 HTTP 클라이언트만
- 틱 수신마다 exit rules 배열 순서대로 평가, first_match 청산
