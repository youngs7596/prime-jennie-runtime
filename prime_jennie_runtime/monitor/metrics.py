"""Prometheus text exposition — Grafana scrape 용.

`prometheus_client` 없이 최소 구현. v3 초기에는 poller 관련 4 개 메트릭만 노출하고
필요에 따라 확장한다.

노출 metric:
- `monitor_poll_success_timestamp_seconds` (Gauge) — 마지막 성공 UNIX ts
- `monitor_poll_failure_timestamp_seconds` (Gauge) — 마지막 실패 UNIX ts
- `monitor_live_positions` (Gauge) — 현재 포지션 수
- `monitor_info` (Gauge, value=1, label `service`) — 서비스 식별.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .poller import LivePositionsPoller


def render_metrics(poller: LivePositionsPoller | None) -> str:
    lines: list[str] = []

    lines.append("# HELP monitor_info Monitor service info")
    lines.append("# TYPE monitor_info gauge")
    lines.append('monitor_info{service="monitor"} 1')

    success = poller.last_success_ts if poller and poller.last_success_ts else 0.0
    failure = poller.last_failure_ts if poller and poller.last_failure_ts else 0.0
    positions = poller.last_positions_count if poller else 0

    lines.append(
        "# HELP monitor_poll_success_timestamp_seconds Last successful /balance poll (unix ts)"
    )
    lines.append("# TYPE monitor_poll_success_timestamp_seconds gauge")
    lines.append(f"monitor_poll_success_timestamp_seconds {success}")

    lines.append(
        "# HELP monitor_poll_failure_timestamp_seconds Last failed /balance poll (unix ts)"
    )
    lines.append("# TYPE monitor_poll_failure_timestamp_seconds gauge")
    lines.append(f"monitor_poll_failure_timestamp_seconds {failure}")

    lines.append("# HELP monitor_live_positions Current watched position count")
    lines.append("# TYPE monitor_live_positions gauge")
    lines.append(f"monitor_live_positions {positions}")

    return "\n".join(lines) + "\n"
