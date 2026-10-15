"""VAANI interrupts: a global STOP hotkey, a cancel flag every long task checks, and spoken "stop".

cancelled()          True after Stop was pressed/said (cleared automatically at the start of the next command)
cancel() / reset()   set / clear the flag
start_hotkey()       registers Ctrl+Alt+X (VAANI_STOP_HOTKEY="ctrl+alt+x") -> stops speech + cancels the running task
"""
from __future__ import annotations

import logging
import os
import sys
import threading

log = logging.getLogger("vaani.interrupt")
_cancel = threading.Event()
_started = False

STOP_WORDS = {"stop", "cancel", "quiet", "enough", "shut up", "be quiet", "stop it", "stop talking", "never mind", "nevermind", "abort"}


def cancelled() -> bool:
    return _cancel.is_set()


def reset() -> None:
    _cancel.clear()


def cancel(reason: str = "stop") -> None:
    log.info("CANCEL requested (%s)", reason)
    _cancel.set()
    try:
        import voice
        voice.stop_speaking()
    except Exception:
        pass


def is_stop_phrase(text: str) -> bool:
    t = " ".join((text or "").lower().replace(".", " ").replace(",", " ").replace("!", " ").split())
    try:
        import wakeword
        t = wakeword.strip_prefix(t)
    except Exception:
        pass
    return t in STOP_WORDS


_MODS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}


def start_hotkey() -> bool:
    """Global hotkey on Windows via RegisterHotKey (no extra packages). Returns True if it was registered."""
    global _started
    if _started or not sys.platform.startswith("win"):
        return False
    spec = os.getenv("VAANI_STOP_HOTKEY", "ctrl+alt+x").lower().replace(" ", "")
    if spec in {"off", "none", "0"}:
        return False
    parts = spec.split("+")
    mods = 0
    for p in parts[:-1]:
        mods |= _MODS.get(p, 0)
    key = parts[-1]
    if len(key) != 1:
        return False
    vk = ord(key.upper())

    def loop():
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 0x5641, mods | 0x4000, vk):          # 0x4000 = MOD_NOREPEAT
            log.warning("could not register stop hotkey %s (already used by another program?)", spec)
            return
        log.info("stop hotkey registered: %s", spec)
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:                                         # WM_HOTKEY
                cancel("hotkey")
    threading.Thread(target=loop, daemon=True, name="VAANI-StopHotkey").start()
    _started = True
    return True