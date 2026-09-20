"""CLI for venue quote capture — MILE-022.

Run continuously in a dedicated terminal::

    python -m forex_research.capture.cli            # bounded by Ctrl+C
    python -m forex_research.capture.cli --cycles 5 # bounded smoke run
    python -m forex_research.capture.report         # episode counts vs minimums
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MILE-022 venue quote capture (read-only)")
    parser.add_argument("--config", type=Path, default=Path("config/capture.yaml"))
    parser.add_argument("--cycles", type=int, default=None, help="bounded run for smoke tests")
    args = parser.parse_args(argv)

    from .service import run_capture

    return run_capture(args.config, max_cycles=args.cycles)


if __name__ == "__main__":
    sys.exit(main())
