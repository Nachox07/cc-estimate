"""Output files."""
from pathlib import Path


def open_out(path, mode="w"):
    """Open an output file, creating its directory first."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return open(path, mode)
