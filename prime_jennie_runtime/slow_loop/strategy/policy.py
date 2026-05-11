"""Strategy Policy loader.

strategy_policy.yaml을 읽어 strategy_tag별 base_pct / max_notional_krw /
기본 exit rules를 제공. Strategy Engine이 시트 조립 시 참조.

RSI_REBOUND 같은 폐기 tag는 정책에 없으므로 조회 시 KeyError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_POLICY_PATH = Path(__file__).parent / "strategy_policy.yaml"


@dataclass(frozen=True)
class StrategyEntry:
    """단일 strategy_tag의 정책."""

    base_pct: float
    max_notional_krw: int
    default_exit_rules: list[dict[str, Any]]
    default_entry_conditions: list[dict[str, Any]]
    # v2 sizing 동등: 자산비례 cap. None 이면 절대값(max_notional_krw)만 사용.
    max_notional_pct: float | None = None


@dataclass(frozen=True)
class StrategyPolicy:
    """로드된 전체 정책. 버전 + tag별 엔트리."""

    version: str
    entries: dict[str, StrategyEntry]

    def get(self, tag: str) -> StrategyEntry:
        """tag에 해당하는 엔트리 반환. 없으면 KeyError."""
        if tag not in self.entries:
            raise KeyError(f"strategy_tag '{tag}' not found in policy")
        return self.entries[tag]

    def has(self, tag: str) -> bool:
        return tag in self.entries


def load_policy(path: Path | str | None = None) -> StrategyPolicy:
    """YAML 파일에서 정책 로드. path=None이면 패키지 내 기본 파일."""
    if path is None:
        path = _DEFAULT_POLICY_PATH
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    entries: dict[str, StrategyEntry] = {}
    for tag, cfg in raw.get("strategies", {}).items():
        entries[tag] = StrategyEntry(
            base_pct=float(cfg["base_pct"]),
            max_notional_krw=int(cfg["max_notional_krw"]),
            default_exit_rules=list(cfg["default_exit_rules"]),
            default_entry_conditions=list(cfg.get("default_entry_conditions", [])),
            max_notional_pct=(
                float(cfg["max_notional_pct"]) if "max_notional_pct" in cfg else None
            ),
        )
    return StrategyPolicy(version=str(raw["policy_version"]), entries=entries)
