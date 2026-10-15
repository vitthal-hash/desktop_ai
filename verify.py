"""VAANI verification: never say "done" unless the screen proves it.

attempt(label, strategies, check) runs each strategy in turn. After every try it looks at the screen again; the first
strategy whose result passes `check` wins. If every strategy fails, the answer is an honest failure (and the caller can
hand the goal to the screen agent).

    strategies = [("extension", fn1), ("mouse wheel", fn2), ("keyboard", fn3)]    each fn() -> (ok, message) | None
    check      = verify.screen_changed()  |  verify.window_closed()  |  verify.window_opened("notepad")  | custom(before, after)

Every attempt is appended to actions.jsonl in VAANI's data folder (what was tried, what changed) for debugging.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import screen_state as ss

log = logging.getLogger("vaani.verify")
Result = tuple


def _cancelled() -> bool:
    try:
        import interrupt
        return interrupt.cancelled()
    except Exception:
        return False


def _log_action(entry: dict) -> None:
    try:
        import paths
        with open(paths.data_dir() / "actions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ checks
def screen_changed(min_pixels: float = 0.004) -> Callable:
    return lambda before, after: ss.diff(before, after, min_pixels)


def window_closed() -> Callable:
    """The window that was in front is gone (or no longer the active one)."""
    def check(before, after):
        t = before["fg"].get("title")
        if t and t not in after["windows"]:
            return True, ["window disappeared"]
        if before["fg"].get("hwnd") != after["fg"].get("hwnd"):
            return True, ["active window changed"]
        return False, []
    return check


def window_opened(fragment: str) -> Callable:
    frag = fragment.lower()

    def check(before, after):
        new = [w for w in after["windows"] if frag in w.lower() and w not in before["windows"]]
        if new or frag in after["fg"].get("title", "").lower():
            return True, [f"window '{(new or [after['fg'].get('title')])[0][:40]}' is open"]
        return False, []
    return check


def foreground_changed() -> Callable:
    def check(before, after):
        if before["fg"].get("hwnd") != after["fg"].get("hwnd") or before["fg"].get("title") != after["fg"].get("title"):
            return True, ["active window changed"]
        return False, []
    return check


def rect_changed() -> Callable:
    def check(before, after):
        if before["fg"].get("rect") != after["fg"].get("rect"):
            return True, ["window moved or resized"]
        return False, []
    return check


def custom_ok(fn: Callable[[], bool], why: str = "confirmed") -> Callable:
    """check that ignores the fingerprints and asks a function (e.g. 'is the volume now 50?')."""
    return lambda before, after: (True, [why]) if fn() else (False, [])


# ------------------------------------------------------------------ engine
def attempt(label: str, strategies: list, check: Callable, settle: float = 0.35, timeout: float = 1.8,
            deep: bool = True) -> tuple:
    """-> (ok, message, trace). message is the successful strategy's own message when ok."""
    trace = []
    for name, fn in strategies:
        if _cancelled():
            return False, "Okay, stopped.", trace
        before = ss.fingerprint(deep)
        try:
            res = fn()
        except Exception as exc:
            log.info("[%s] strategy %s crashed: %s", label, name, exc)
            trace.append({"strategy": name, "error": str(exc)[:100]})
            continue
        if res is not None and isinstance(res, tuple) and res and res[0] is False:
            trace.append({"strategy": name, "reported": str(res[1])[:100]})
            continue
        time.sleep(settle)
        deadline = time.monotonic() + timeout
        ok, why = False, []
        while True:
            after = ss.fingerprint(deep)
            ok, why = check(before, after)
            if ok or time.monotonic() >= deadline or _cancelled():
                break
            time.sleep(0.2)
        trace.append({"strategy": name, "verified": ok, "evidence": why})
        if ok:
            msg = res[1] if isinstance(res, tuple) and len(res) > 1 else "Done."
            _log_action({"t": time.time(), "label": label, "ok": True, "via": name, "evidence": why})
            return True, msg, trace
        log.info("[%s] %s ran but nothing changed; trying the next way", label, name)
    _log_action({"t": time.time(), "label": label, "ok": False, "trace": trace})
    return False, f"I tried {len(strategies)} way{'s' if len(strategies) != 1 else ''} to {label}, but I couldn't see it happen.", trace