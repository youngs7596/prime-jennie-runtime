"""scripts/seed_scheduled_jobs.py — SEEDS 형식/유효성 정적 검증.

실제 DB 연결 없이, 시드 레코드가:
- 고유 id 를 가지는지
- cron 이 apscheduler CronTrigger 로 파싱되는지
- handler_key 가 비어있지 않은지
"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

from scripts.seed_scheduled_jobs import SEEDS


def test_seed_ids_are_unique():
    ids = [s.id for s in SEEDS]
    assert len(ids) == len(set(ids))


def test_seed_crons_parse():
    for s in SEEDS:
        CronTrigger.from_crontab(s.cron)  # raises on invalid


def test_seed_handler_key_non_empty():
    for s in SEEDS:
        assert s.handler_key


def test_seed_owner_matches_id_prefix():
    """id 컨벤션: '{owner}.{handler_key}' (사람이 읽기 편하게)."""
    for s in SEEDS:
        assert s.id.startswith(f"{s.owner}.")
