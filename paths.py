"""Where VAANI keeps its data (memory, routines, timers, action log). Same folder as vaani.log."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    p = Path(os.getenv("VAANI_DATA_DIR", str(Path.home() / "AppData" / "Local" / "VAANI")))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path.home() / ".vaani"
        p.mkdir(parents=True, exist_ok=True)
    return p