"""VAANI feature router: dictation, memory, timers/reminders, routines, window management, system info.

handle(text, run_command) -> Result | None
    run_command(cmd) -> Result  executes one ordinary command (used by routines)
silent replies: a Result whose message is "" means "do not speak" (used while dictating).
"""
from __future__ import annotations

import logging
import re

import longmem
import routines
import sysinfo
import timers
import winmgr

log = logging.getLogger("vaani.features")
Result = tuple
_dictating = False


def dictating() -> bool:
    return _dictating


def _type(text: str) -> Result:
    import tools
    return tools._clipboard_paste(text)


def _dictation(text: str):
    global _dictating
    low = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?")
    if re.fullmatch(r"(?:please )?(?:start|begin|enable|turn on|go into|enter)(?: the)? (?:dictation|typing)(?: mode)?|dictation mode|dictate", low):
        _dictating = True
        return True, "Dictation on. I'll type whatever you say. Say stop dictation when you're done."
    if _dictating:
        if re.fullmatch(r"(?:please )?(?:stop|end|finish|exit|turn off|cancel)(?: the)? (?:dictation|typing)(?: mode)?|that's all|that is all|stop dictating", low):
            _dictating = False
            return True, "Dictation off."
        if re.fullmatch(r"(?:scratch|delete|undo) that", low):
            import pyautogui
            pyautogui.hotkey("ctrl", "z")
            return True, ""
        if re.fullmatch(r"press enter|new line please|send it", low):
            import pyautogui
            pyautogui.press("enter")
            return True, ""
        formatted = sysinfo.format_dictation(text)
        if formatted and not formatted.endswith(("\n", " ")) and not re.search(r"[.!?:]$", formatted):
            pass
        ok, msg = _type(formatted + " ")
        return (ok, "" if ok else msg)
    return None


def handle(text: str, run_command):
    """First feature that recognises the sentence answers it; None = not mine."""
    global _dictating
    d = _dictation(text)
    if d is not None:
        return d
    for name, fn in (("memory", longmem.handle), ("timers", timers.handle), ("windows", winmgr.handle), ("system", sysinfo.handle)):
        try:
            r = fn(text)
        except Exception as exc:
            log.exception("%s feature crashed", name)
            r = (False, f"My {name} feature hit a problem: {str(exc)[:80]}")
        if r is not None:
            log.info("FEATURE %s: %r => %r", name, text, r)
            return r
    try:
        r = routines.handle(text, run_command)
    except Exception as exc:
        log.exception("routines crashed")
        r = (False, f"My routines hit a problem: {str(exc)[:80]}")
    if r is not None:
        log.info("FEATURE routines: %r => %r", text, r)
        return r
    return None