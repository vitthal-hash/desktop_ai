"""VAANI listening chime: a short two-note "ding" when it wakes up, like a smart speaker, so you know it is listening.
Turn off with VAANI_CHIME=0.  Never blocks and never raises."""
from __future__ import annotations

import os
import sys
import threading


def _play(notes) -> None:
    try:
        import winsound
        for freq, ms in notes:
            winsound.Beep(freq, ms)
    except Exception:
        pass


def _enabled() -> bool:
    return sys.platform.startswith("win") and os.getenv("VAANI_CHIME", "1").strip().lower() not in {"0", "off", "false", "no"}


def listening() -> None:
    if _enabled():
        threading.Thread(target=_play, args=(((880, 70), (1319, 110)),), daemon=True, name="VAANI-Chime").start()