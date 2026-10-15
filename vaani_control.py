"""Shared control state for VAANI: pause/resume, shutdown, status, sleep/resume detection, single instance."""
from __future__ import annotations

import logging
import sys
import threading
import time

log = logging.getLogger("vaani.control")


class ListeningPaused(Exception):
    """Raised inside listening loops when the microphone has been switched off."""


_shutdown = threading.Event()
_paused = threading.Event()
_resumed = threading.Event()
_lock = threading.Lock()
_status = ("starting", "Starting VAANI")
_subscribers: list = []
_mutex_handle = None


def shutdown_event() -> threading.Event:
    return _shutdown


def is_paused() -> bool:
    return _paused.is_set()


def request_pause(flag: bool = True) -> None:
    (_paused.set if flag else _paused.clear)()
    if flag:
        set_status("paused", "Microphone off - use the tray menu to resume the microphone")
    else:
        set_status("wake", f'Waiting for "{__import__("wakeword").display()}"')
    log.info("microphone %s", "paused" if flag else "resumed")


def toggle_pause() -> bool:
    """Flip the manual mic switch (used by the tray menu). Returns True if now paused."""
    request_pause(not is_paused())
    return is_paused()


def request_shutdown() -> None:
    _shutdown.set()


def set_status(state: str, detail: str = "") -> None:
    global _status
    with _lock:
        _status = (state, detail)
        subs = list(_subscribers)
    for cb in subs:
        try:
            cb(state, detail)
        except Exception:
            pass


def get_status() -> tuple:
    with _lock:
        return _status


def subscribe(callback) -> None:
    with _lock:
        _subscribers.append(callback)


# ---- sleep / resume detection ------------------------------------------------
def note_resume() -> None:
    _resumed.set()


def consume_resume() -> bool:
    """True once after the PC woke from sleep (or the session was locked for a long time)."""
    if _resumed.is_set():
        _resumed.clear()
        return True
    return False


def start_power_watch(poll_seconds: float = 2.0, jump_seconds: float = 15.0) -> threading.Thread:
    """Detect sleep/hibernate: the wall clock jumps far more than our sleep interval."""
    def run():
        last = time.time()
        while not _shutdown.is_set():
            time.sleep(poll_seconds)
            now = time.time()
            if now - last > jump_seconds:
                log.info("power watch: clock jumped %.0fs -> resumed from sleep", now - last)
                note_resume()
            last = now
    t = threading.Thread(target=run, daemon=True, name="VAANI-PowerWatch")
    t.start()
    return t


# ---- single instance ---------------------------------------------------------
def single_instance(name: str = "Global\\VAANI_Assistant_Mutex") -> bool:
    """False if another VAANI is already running (so startup triggers can never create duplicates)."""
    global _mutex_handle
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS
    except Exception:
        return True