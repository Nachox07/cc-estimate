"""Output files."""
import json
from datetime import datetime
from pathlib import Path


def open_out(path, mode="w"):
    """Open an output file, creating its directory first."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return open(path, mode)


def write_since(tasks, since):
    """Record the left edge of the fetched window beside a tasks file, so backtest and estimate cut at it.
    Always written (null when unknown), so a stale file from an earlier run cannot survive."""
    with open_out(f"{tasks}.meta.json") as f:
        json.dump({"since": since.isoformat() if since else None}, f)


def read_since(tasks):
    try:
        return datetime.fromisoformat(json.loads(Path(f"{tasks}.meta.json").read_text())["since"])
    except (OSError, KeyError, ValueError, TypeError):
        return None
