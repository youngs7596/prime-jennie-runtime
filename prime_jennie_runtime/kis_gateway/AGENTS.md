# `kis_gateway/` — KIS API 프록시

Track C 소유. **v2 가장 안정적인 서비스. 그대로 포팅.**

## v2 원본

`prime_jennie/services/gateway/` — app.py, kis_api.py, streamer.py, poller.py

## 핵심 컴포넌트

- FastAPI 서버 (별도 컨테이너)
- 토큰 매니저: 24h 자동 갱신, 파일 캐시
- Rate limiter: 시세 19/sec, 매매 5/sec
- Circuit breaker: fail_max=20, reset=60s
- WebSocket streamer + REST polling fallback

## 절대 금지

- **장중(09:00~15:30) 재시작 금지** — 토큰 캐시 소실 시 rate limit(403)
- 이 서비스만 KIS OpenAPI와 직접 통신. 다른 서비스는 이 gateway HTTP API만 호출
