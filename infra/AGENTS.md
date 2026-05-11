# infra/ — 관측/터널 인프라 설정

v2 `prime-jennie/infra/` 포팅. docker-compose 에서 bind mount 로 소비.

## 디렉토리

- `loki/loki-config.yaml` — Loki 2.9 (단일 노드, tsdb, 30d retention)
- `promtail/promtail-config.yaml` — 도커 컨테이너 stdout 수집 → Loki
- `grafana/provisioning/datasources/datasource.yaml` — Loki/Prometheus 자동 등록
- `grafana/provisioning/dashboards/` — pj_overview / pj_pnl / pj_macro 대시보드 + provider 설정
- `prometheus/prometheus.yml` — monitor `/metrics` scrape + alert rules 로드
- `prometheus/rules/alert.rules.yml` — MonitorPollStale / PositionStateStale / ServiceTargetDown
- `cloudflared/` — Cloudflare Tunnel 설정 (token 기반, 현재 파일 없음)

## v2 → v3 변경

v2 는 `network_mode: host` 였다. v3 는 **bridge** 네트워크이므로:

- promtail clients.url: `http://localhost:3100` → `http://loki:3100`
- grafana datasource url: `http://localhost:3100` → `http://loki:3100`

## 소유권

- Track E (track-e-infra): 이 디렉토리 전체 소유
- 다른 Track: read-only. 새 datasource/dashboard 추가 요청 시 Track E 에 PR
