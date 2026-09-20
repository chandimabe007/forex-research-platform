"""Capture configuration — COST-013, DATA-013.

Session zone and continuity thresholds are provisional UTC values until the
server zone is verified (GATE-005) and thresholds are calibrated from
observed inter-quote intervals (DATA-013).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CaptureConfig:
    symbols: tuple[str, ...]
    output_dir: Path
    continuity_threshold_ms: dict[str, int]  # per session bucket (provisional)
    episode_minimums: dict[str, int]  # COST-013 independent episode minimums
    poll_interval_s: float = 0.05
    flush_interval_s: float = 5.0

    def threshold_for(self, bucket: str) -> int:
        return self.continuity_threshold_ms.get(bucket, 60_000)


def load_capture_config(path: Path) -> CaptureConfig:
    if not path.exists():
        raise FileNotFoundError(f"capture config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    config = CaptureConfig(
        symbols=tuple(data.get("symbols", ())),
        output_dir=Path(data.get("output_dir", "data/raw/venue_quotes")),
        continuity_threshold_ms=dict(data.get("continuity_threshold_ms", {})),
        episode_minimums=dict(data.get("episode_minimums", {})),
        poll_interval_s=float(data.get("poll_interval_s", 0.05)),
        flush_interval_s=float(data.get("flush_interval_s", 5.0)),
    )
    if not config.symbols:
        raise ValueError("capture.yaml must list at least one symbol")
    return config
