"""Manifests and versioning — DATA-020.

Every promotion writes a manifest: source, date range, row count, gap list,
SHA-256 of the cleaned output, validation report, ingestion git commit. That
SHA is the data version cited by experiments. Two experiments citing
different SHAs are not comparable.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .validate import ValidationReport


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(repo: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def write_manifest(
    *,
    out_dir: Path,
    source: str,
    symbol: str,
    timeframe: str,
    date_from: datetime,
    date_to: datetime,
    row_count: int,
    gap_list: list[str],
    cleaned_path: Path,
    validation: ValidationReport,
    repo: Path | None = None,
) -> Path:
    """Write the promotion manifest; returns its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "written_at": datetime.now(UTC).isoformat(),
        "source": source,
        "symbol": symbol,
        "timeframe": timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "row_count": row_count,
        "gap_list": gap_list,
        "cleaned_sha256": sha256_file(cleaned_path),
        "validation": json.loads(validation.to_json()),
        "ingestion_git_commit": git_commit(repo or Path.cwd()),
    }
    path = out_dir / f"manifest_{symbol}_{timeframe}_{date_from:%Y%m%d}_{date_to:%Y%m%d}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path
