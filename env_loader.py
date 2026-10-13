"""Loads the project's .env file (KEY=VALUE lines) into the environment - no extra packages needed.
Imported automatically by tools.py, gemini_planner.py and screen_vision.py, so GEMINI_API_KEY in .env always works,
including when VAANI is started by start_vaani.bat, pythonw or Task Scheduler.
Real environment variables (setx) win over .env unless they are empty."""
from __future__ import annotations

import os
from pathlib import Path

_loaded = False


def load_env() -> list:
    global _loaded
    names = []
    for folder in (Path(__file__).resolve().parent, Path.cwd()):
        path = folder / ".env"
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
                if key and value and not os.environ.get(key):
                    os.environ[key] = value
                    names.append(key)
        except Exception:
            pass
    _loaded = True
    return names


load_env()