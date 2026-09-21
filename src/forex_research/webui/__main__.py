"""python -m forex_research.webui — serve the status dashboard (default port 8787)."""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
