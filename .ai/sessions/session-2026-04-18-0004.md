# 세션 핸드오프 — 2026-04-18 (복구 세션)

**세션 범위**: UI 전면 공백 신고 진단 → 누적 버그 5건 + 월요일 장 침묵 직전 스케줄러 함정 1건 일괄 수정
**시작**: 2026-04-18 오후 (Phase 2.13 핸드오프 직후) · **마감**: 같은 날 저녁
**팀**: lead 단독 (Opus 4.7 1M)
**운영 영향**: 월요일(2026-04-20) 장 시작 전에 apscheduler dow 함정을 잡음 — 안 잡혔다면 월요일 장중 macro/scout/minute_chart 전량 누락 예정

## 시작 상태 진단

사용자 리포트 "UI 에 아무런 데이터도 안 나온다". 로컬 노트북이 아닌 **MS-01 control-ui** 에서 목격. 11 서비스 healthy 로 보이는데 화면만 공백.

## 발견된 이슈 맵 (5 + 1)

| # | 증상 | 근본 원인 | 범위 |
|---|---|---|---|
| 1 | control-ui → `/api/*` **502 Bad Gateway** | nginx 가 startup 시점 DNS 결과(`dashboard:8090` → `172.18.0.8`) 를 영구 캐시. dashboard 재생성 후 IP 가 `172.18.0.17` 로 바뀌었지만 옛 IP 로 connect refused | control-ui 전 화면 공백 |
| 2 | monitor 전량 404, Portfolio 잔고 비어 있음 | `monitor/poller.py`, `dashboard/routers/portfolio.py` 가 `/balance` 호출. kis-gateway 실제 라우트는 `/api/balance` | monitor 기능 완전 무효, Portfolio 페이지 |
| 3 | UI Logs 가 `Invalid Date` 표기 | 백엔드가 Loki nanosecond timestamp(`"1776483188436887066"`) 를 그대로 전달. 프론트 `new Date(ts)` 가 NaN | Logs 페이지 |
| 4 | news-pipeline kure embed 전량 실패 + litellm `127.0.0.1:8001 ConnectError` | `.env` 의 VLLM/QDRANT URL 이 `127.0.0.1` (v2 host-mode 시절 잔재). v3 bridge 컨테이너에선 자기 자신을 가리킴. **host.docker.internal / LAN IP 모두 ufw INPUT default deny (routed) 로 차단** | 임베딩 0건, EXAONE 호출 실패 |
| 5 | 방화벽 수정만으로 근본적이지 않은 구조 차이 | v2 잔존 서비스(vllm/qdrant) 가 `network_mode: host`, v3 서비스는 bridge — L2 단절 + ufw INPUT 격리 | (상위 근본 원인) |
| 6 | **토요일에 `1-5` 스케줄 다 실행됨 + 월요일엔 침묵 예정** | `CronTrigger.from_crontab` 가 cron 표준(0/7=Sun, 1=Mon) 이 아닌 apscheduler 고유 규약(0=Mon ... 6=Sun) 으로 dow 해석. `1-5` → 의도된 월-금 대신 **화-토** 로 해석 | Phase 2.9 slice2 이후 모든 `dow` 가 있는 스케줄이 하루씩 밀려 있었음 |

## 수정 내용 (커밋)

### prime-jennie-runtime

```
1c30390 fix(scheduler): cron dow 를 apscheduler 규약으로 변환 — 월요일 장중 누락 방지
2f308ae fix(monitor,dashboard): KIS Gateway balance 경로를 /api/balance 로 정정
```

- **`infra/scheduler.py`**: `_normalize_cron_for_apscheduler` 추가. `from_crontab` 호출 전 dow 필드만 치환 (단일/범위/리스트/스텝/이름 mon-fri 전부 지원). DB 는 cron 표준 유지.
- **`monitor/poller.py`, `dashboard/routers/portfolio.py`**: `/balance` → `/api/balance`.
- **tests**: scheduler 6 건(월-금 범위, 일요일 0/7 두 형태, 리스트+이름, Tue-Sat, 와일드카드, 비5필드), monitor test_poller mock URL 수정.

### prime-jennie-control-ui

```
4e8d7b4 fix(Logs): Loki nanosecond timestamp 인식 — Invalid Date 표기 해소
9f88136 fix(nginx): Docker resolver + 변수 proxy_pass — 컨테이너 재생성 시 502 방지
```

- **`nginx.conf`**: `resolver 127.0.0.11 valid=10s ipv6=off;` + `set $upstream_dashboard ... ; proxy_pass $upstream_dashboard$request_uri;` — 요청마다 DNS 재해결.
- **`src/pages/Logs.tsx`**: `fmtTime` 이 16자리 이상 숫자 문자열이면 `BigInt(ts) / 1_000_000n` 로 ms 환산 후 Date 생성.

### prime-jennie (v2 compose)

```
5ca4dee infra(compose): qdrant/vllm 을 bridge 모드 + prime-jennie-runtime_default 공유   (development)
```

- qdrant / vllm-llm / vllm-embed 세 서비스에서 `network_mode: host` 제거.
- `ports: "127.0.0.1:6333/6334/8001/8002:..."` publish (호스트에서 smoke 는 가능, LAN 노출 없음).
- 파일 끝 `networks:` 에 `default: {name: prime-jennie_default}` + `runtime_shared: {external: true, name: prime-jennie-runtime_default}` 선언. 세 서비스가 양 네트워크에 참여.
- v3 `.env` 의 VLLM_LLM_URL / VLLM_EMBED_URL / QDRANT_URL 을 container name 기반으로 교정 (`http://prime-jennie-vllm-llm-1:8001/v1` 등). `.env.bak.before-d1` 백업 보존.

## 배포 & 검증

- runtime repo main push 후 `docker compose build` (6 서비스) + `up -d --profile full` 로 전 컨테이너 recreate
- v2 compose 적용 시 qdrant/vllm 3개 recreate → EXAONE 재로드 약 30초 (HF cache 덕)
- **컨테이너에서 vllm/qdrant 접근 검증**: news-pipeline / slow-loop → `LLM/EMBED/QDRANT` 전부 HTTP 200
- **control-ui 내성 검증**: dashboard 를 `docker compose up -d --force-recreate` 로 교체해도 nginx 502 없이 통과
- **스케줄러 dow 수정 검증**: 13:32 KST 에 job-worker 재기동 후 13:35 KST 토요일 tick 에서 **job_runs 0 rows** — 더 이상 토요일에 실행되지 않음을 실측 확인
- 임시 iptables 룰(`DOCKER-USER -s 172.18.0.0/16 -j ACCEPT`) 제거 후에도 bridge 경로 정상 → 방화벽 수정 불필요가 확인됨

## 결정 이력

1. **방화벽 allow 대신 네트워크 구조 정비** (2026-04-18)
   - 이유: v2 시절 host-mode 일원화로 원래 문제가 없었던 구조. ufw 뚫는 건 symptom 치료, bridge 일원화가 근본 해결
   - 영향: 재부팅·재배포 시에도 방화벽 지식 없이 무조건 동작
2. **localhost-only ports publish** (127.0.0.1 바인딩)
   - 이유: v3 는 container name 경로만 쓰면 충분. host smoke 편의상 publish 는 유지
   - 영향: LAN 외부에서 직접 vllm 접근 불가 — 보안 기본값 좋은 쪽
3. **cron 표준 저장, apscheduler 변환은 런타임** (2026-04-18)
   - 이유: DB 값이 읽기 쉬워야 control-ui 가 CRUD 하기 편함. cron 표준이 보편적
   - 영향: 다음에 seed 수정할 때 `1-5` 를 그대로 쓰면 월-금 의도가 정확히 반영됨
4. **nginx resolver + 변수 proxy_pass 를 영구 패턴화**
   - 이유: docker 배포 반복 시 재발이 잦은 원인 한 줄로 제거
   - 영향: 향후 다른 컨테이너(프록시 대상) 추가 시 같은 패턴 권장

## 후속 (월요일 장 시작 전/직후)

- **월요일 09:00 KST 첫 tick 관측**: `*/5 9-15 * * 1-5` 와 `30 8-14 * * 1-5` 가 **월요일에 정상 점화** 확인. 안 돌면 scheduler 수정 회귀
- **seed 스크립트 주석**: `scripts/seed_scheduled_jobs.py` 상단에 "cron 표준으로 저장, scheduler 가 apscheduler 규약으로 변환" 한 줄 추가 권장
- **minyoung-mah PyPI 0.1.2 publish** (사용자 액션 대기)
- **Heartbeat interval/TTL env 변수화** (Phase 2.13 이월)
- **Shadow Macro 누적** — 2-3주 후(2026-05-05~) 모델 스위치 결정

## 참고 명령

- scheduler 재시험: `docker exec prime-jennie-runtime-postgres-1 psql -U pj_admin -d prime_jennie_v3 -c "SELECT job_id, started_at AT TIME ZONE 'Asia/Seoul' as kst, status FROM scheduled_job_runs WHERE started_at >= now() - interval '10 min' ORDER BY started_at DESC"`
- v3 network 참여 목록: `docker network inspect prime-jennie-runtime_default --format '{{range .Containers}}{{.Name}} {{end}}'`
- vllm 경유 테스트: `docker exec prime-jennie-runtime-news-pipeline-1 python3 -c "import httpx; print(httpx.get('http://prime-jennie-vllm-llm-1:8001/v1/models', timeout=3).status_code)"`
