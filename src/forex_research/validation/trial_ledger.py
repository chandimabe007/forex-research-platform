"""Trial ledger — VAL-045: the feed for ``s_ledger`` in VAL-040.

Three-part universe, never conflated (spec: VAL-045):

- **Audit count** — every trial appended, including failures. "Failed
  experiments are never deleted. They are the denominator. A ledger of
  successes is a record of survivorship."
- **Statistical candidate set** — only return series that competed under
  the *same selection criterion* and are *comparable* (same fill mode).
  This is the N and the ``s_ledger`` source for VAL-040.
- A feature created and discarded without producing a return series has
  no Sharpe observation and belongs to neither — registration tooling
  outside this module tracks that distinction.

Storage: append-only JSONL at ``data/trials/ledger.jsonl``, one record per
line, each carrying ``prev_hash`` and ``record_hash`` — tamper-evident by
chained SHA-256, per the spec's own prescription ("back it with chained
record hashes"). Tamper-evident, not tamper-proof: a determined attacker
with write access can rewrite the whole chain; the point is that *any*
accidental or selective edit, deletion or reordering is detectable.

Every record states which rule produced it and the fill mode — "a backtest
with approximate fills is not comparable with one with exact fills, and
the ledger must distinguish them."
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .deflated_sharpe import SR0, sr0_empirical

__all__ = [
    "TrialLedger",
    "TrialRecord",
    "GENESIS_HASH",
]

GENESIS_HASH = "0" * 64
_HASH_FIELDS = (
    "trial_id",
    "created_utc",
    "candidate_id",
    "feature_version",
    "selection_criterion",
    "fill_mode",
    "rule",
    "sr_periodic",
    "n_observations",
    "notes",
    "prev_hash",
)


@dataclass(frozen=True)
class TrialRecord:
    """One Sharpe observation from one candidate that competed.

    ``sr_periodic`` is the periodic Sharpe (matching the observation
    frequency), never annualised — the same unit discipline as VAL-040.
    """

    trial_id: str
    created_utc: str
    candidate_id: str
    feature_version: str
    selection_criterion: str
    fill_mode: str
    rule: str
    sr_periodic: float
    n_observations: int
    notes: str = ""
    prev_hash: str = GENESIS_HASH
    record_hash: str = field(default="")

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.candidate_id.strip():
            errors.append("candidate_id is required")
        if not self.feature_version.strip():
            errors.append(
                "feature_version is required (changing a computation invalidates entries citing it)"
            )
        if not self.selection_criterion.strip():
            errors.append("selection_criterion is required (comparability key, VAL-045)")
        if not self.fill_mode.strip():
            errors.append("fill_mode is required (exact vs approximate fills are not comparable)")
        if not self.rule.strip():
            errors.append("rule is required (every result records which rule produced it)")
        if not math.isfinite(self.sr_periodic):
            errors.append("sr_periodic must be finite")
        if not isinstance(self.n_observations, int) or self.n_observations < 2:
            errors.append("n_observations must be an integer >= 2 (a Sharpe needs variance)")
        return errors


def _hash_record(record: TrialRecord) -> str:
    payload = json.dumps(
        {name: getattr(record, name) for name in _HASH_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TrialLedger:
    """Append-only, hash-chained ledger of Sharpe trials (VAL-045)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- reading -------------------------------------------------------------

    def entries(self) -> list[TrialRecord]:
        """All appended trials, oldest first. Never filters, never hides."""
        if not self._path.exists():
            return []
        out: list[TrialRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            out.append(TrialRecord(**raw))
        return out

    def audit_count(self) -> int:
        """Every trial ever appended — the honest denominator."""
        return len(self.entries())

    def statistical_candidate_set(
        self,
        *,
        selection_criterion: str,
        fill_mode: str,
        feature_version: str | None = None,
    ) -> list[TrialRecord]:
        """Only comparable trials: same criterion, same fill mode (VAL-045).

        These — and only these — feed ``N`` and ``s_ledger`` in VAL-040.
        """
        return [
            e
            for e in self.entries()
            if e.selection_criterion == selection_criterion
            and e.fill_mode == fill_mode
            and (feature_version is None or e.feature_version == feature_version)
        ]

    def verify_chain(self) -> list[str]:
        """Re-walk the hash chain; return every inconsistency found.

        Detects any edit, deletion, insertion or reordering of past
        records. An empty or missing file is a valid (virgin) ledger.
        """
        errors: list[str] = []
        prev = GENESIS_HASH
        for position, record in enumerate(self.entries(), start=1):
            if record.prev_hash != prev:
                errors.append(
                    f"record {position} ({record.trial_id}): prev_hash does not match "
                    "the chain — the ledger was edited, reordered or has a gap"
                )
            expected = _hash_record(record)
            if record.record_hash != expected:
                errors.append(
                    f"record {position} ({record.trial_id}): record_hash mismatch — "
                    "the record's contents were modified after appending"
                )
            prev = record.record_hash
        return errors

    # -- appending -----------------------------------------------------------

    def append(self, record: TrialRecord) -> TrialRecord:
        """Append one trial. Refuses invalid records and chain mismatches."""
        errors = record.validate()
        if errors:
            raise ValueError("trial record invalid: " + "; ".join(errors))
        prev = self._tail_hash()
        if record.prev_hash != prev:
            raise ValueError(
                f"prev_hash does not match the ledger tail ({record.prev_hash!r} != {prev!r}) "
                "— append the tail's hash, never a hand-written one"
            )
        complete = TrialRecord(**{**asdict(record), "prev_hash": prev})
        complete = TrialRecord(**{**asdict(complete), "record_hash": _hash_record(complete)})
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(complete), sort_keys=True) + "\n")
        return complete

    def new_record(
        self,
        *,
        candidate_id: str,
        feature_version: str,
        selection_criterion: str,
        fill_mode: str,
        rule: str,
        sr_periodic: float,
        n_observations: int,
        notes: str = "",
        trial_id: str | None = None,
        created_utc: str | None = None,
    ) -> TrialRecord:
        """A pre-filled record whose prev_hash already points at the tail."""
        return TrialRecord(
            trial_id=trial_id if trial_id is not None else uuid.uuid4().hex,
            created_utc=created_utc if created_utc is not None else datetime.now(UTC).isoformat(),
            candidate_id=candidate_id,
            feature_version=feature_version,
            selection_criterion=selection_criterion,
            fill_mode=fill_mode,
            rule=rule,
            sr_periodic=sr_periodic,
            n_observations=n_observations,
            notes=notes,
            prev_hash=self._tail_hash(),
        )

    # -- statistics that feed VAL-040 -----------------------------------------

    def s_ledger(
        self,
        *,
        selection_criterion: str,
        fill_mode: str,
        feature_version: str | None = None,
    ) -> tuple[float, int]:
        """Dispersion of the comparable trial Sharpes, and the count.

        Raises with the VAL-041 routing hint when fewer than two
        comparable trials exist — that is the modelled route's job
        (``sr0_modelled`` with a measured rho), not a job for an assumed
        dispersion.
        """
        trials = self.statistical_candidate_set(
            selection_criterion=selection_criterion,
            fill_mode=fill_mode,
            feature_version=feature_version,
        )
        n = len(trials)
        if n < 2:
            raise ValueError(
                f"only {n} comparable trial(s) — s_ledger needs >= 2; "
                "use the modelled route (sr0_modelled with a measured rho, VAL-041)"
            )
        values = [t.sr_periodic for t in trials]
        mean = math.fsum(values) / n
        variance = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
        return math.sqrt(variance), n

    def sr0(
        self,
        *,
        selection_criterion: str,
        fill_mode: str,
        feature_version: str | None = None,
    ) -> SR0:
        """The VAL-040 hurdle from real trials: s_ledger * E[max of N iid]."""
        s, n = self.s_ledger(
            selection_criterion=selection_criterion,
            fill_mode=fill_mode,
            feature_version=feature_version,
        )
        return sr0_empirical(s_ledger=s, n_trials=n)

    # -- internals -------------------------------------------------------------

    def _tail_hash(self) -> str:
        entries = self.entries()
        return entries[-1].record_hash if entries else GENESIS_HASH
