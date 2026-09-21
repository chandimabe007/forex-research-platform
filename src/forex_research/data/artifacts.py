"""Fitted artefacts carry availability — DATA-021.

Gap tables, cost models, correlation matrices, scalers, volatility
percentiles and regime thresholds are **fitted quantities**. Every fitted
table carries ``calibrated_from``, ``calibrated_to``, ``available_from``,
and one of: frozen (fitted on development data only, stamped, used
unchanged) or causally updated (refitted on an expanding window during
replay). ``VAL-064`` asserts no backtest reads a table whose
``available_from`` postdates the bar being processed.

An unstamped artefact is treated as unavailable (VAL-064 fixture).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import polars as pl


class UpdatePolicy(StrEnum):
    FROZEN = "frozen"
    CAUSALLY_UPDATED = "causally_updated"


class ArtefactUnavailable(RuntimeError):
    """A read violated the availability rules (DATA-021, VAL-064)."""


@dataclass
class FittedArtefact:
    """A fitted table wrapped with its availability stamps."""

    name: str
    frame: pl.DataFrame
    calibrated_from: datetime
    calibrated_to: datetime
    available_from: datetime | None
    policy: UpdatePolicy

    def __post_init__(self) -> None:
        if self.available_from is None:
            raise ArtefactUnavailable(
                f"artefact '{self.name}' has no available_from stamp — an unstamped "
                "artefact is treated as unavailable (DATA-021, VAL-064)"
            )

    def read(self, *, at: datetime) -> pl.DataFrame:
        """Read as of ``at``. Refuses reads that postdate availability.

        In causal-replay mode this is the enforcement point for VAL-064.
        """
        if at < self.available_from:
            raise ArtefactUnavailable(
                f"artefact '{self.name}' available_from={self.available_from.isoformat()} "
                f"postdates the requested bar time {at.isoformat()} — read refused "
                "(DATA-021, VAL-064)"
            )
        return self.frame

    def read_expanding(self, *, at: datetime, refit) -> pl.DataFrame:
        """Causally updated policy: resolve to the version available at ``at``.

        ``refit(calibrated_to) -> pl.DataFrame`` refits on the expanding
        window ending at ``calibrated_to``; the caller supplies it because
        the fitting procedure belongs to the artefact's producer.
        """
        if self.policy is not UpdatePolicy.CAUSALLY_UPDATED:
            return self.read(at=at)
        effective_to = min(at, self.calibrated_to)
        return refit(effective_to)


def save_artefact(artefact: FittedArtefact, path: Path) -> None:
    import json

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    artefact.frame.write_parquet(path)
    sidecar = path.with_suffix(".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "name": artefact.name,
                "calibrated_from": artefact.calibrated_from.isoformat(),
                "calibrated_to": artefact.calibrated_to.isoformat(),
                "available_from": artefact.available_from.isoformat(),
                "policy": artefact.policy.value,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_artefact(path: Path) -> FittedArtefact:
    import json

    path = Path(path)
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    if meta.get("available_from") is None:
        raise ArtefactUnavailable(
            f"artefact '{meta.get('name')}' missing available_from — refused (VAL-064)"
        )
    return FittedArtefact(
        name=meta["name"],
        frame=pl.read_parquet(path),
        calibrated_from=datetime.fromisoformat(meta["calibrated_from"]),
        calibrated_to=datetime.fromisoformat(meta["calibrated_to"]),
        available_from=datetime.fromisoformat(meta["available_from"]),
        policy=UpdatePolicy(meta["policy"]),
    )
