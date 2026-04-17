# `infra/` — 인프라 레이어

Track A 소유. 다른 Track은 이 디렉토리를 **read-only**로 소비합니다.

## 파일별 역할

| 파일 | 책임 | 건드릴 때 주의 |
|---|---|---|
| `config.py` | Pydantic Settings (Postgres/Redis/LLM/KIS/Telegram) | 새 섹션 추가 시 `.env.example` 동기화 |
| `db.py` | async SQLAlchemy 엔진 + 세션 팩토리 (asyncpg) | 커넥션 풀 설정 변경 시 부하 테스트 |
| `redis_streams.py` | `TypedStreamPublisher[T]` / `TypedStreamConsumer[T]` | v2 포팅 기반, at-most-once ACK 패턴 유지 |
| `observer_impl.py` | `StructlogPJObserver` + `CompositePJObserver` | minyoung-mah Observer 프로토콜 준수 |
| `litellm_config.py` | LiteLLM 초기화, 4 tier → 모델 매핑, Langfuse callback | tier 추가 시 `config.py::LLMConfig`도 갱신 |

## 설계 원칙

1. **async 기본**: 모든 I/O는 async. 동기 호출은 `asyncio.to_thread`로 감싸기
2. **v2 패턴 존중**: Redis Streams는 v2 `prime_jennie.infra.redis.streams`의 async 포팅
3. **Langfuse ≠ 라이브러리 의존**: LLM-level trace는 LiteLLM callback, orchestration-level은 Observer

## 스트림 이름 상수

v2와 같은 Redis에서 공존할 수 있도록 `v3:` 접두사 사용:
- `v3:position_sheets` — 포지션 시트 발행
- `v3:position_sheets.dlq` — 검증 실패 시트
