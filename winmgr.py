"""VAANI window management: "snap this to the left", "move this to my other monitor", "next desktop", "minimize everything".

Uses the Windows shortcuts (Win+Arrows etc.), then PROVES the window really moved/changed through verify.attempt.
parse(text) -> (op, label) or None        handle(text) -> Result or None
"""
from __future__ import annotations

import re
import sys

import screen_state as ss
import verify

Result = tuple

_PATTERNS = [
    (r"(?:snap|move|put|dock|send)(?: this| the| my)?(?: current)?(?: window| app)?(?: over)? (?:to )?(?:the )?(left|right)(?: side| half)?", "snap"),
    (r"(?:snap|put|move|dock|send)(?: this| the| my)?(?: current)?(?: window| app)? (?:to |on |at )?(?:the )?(top|bottom)(?: half)?", "snaptb"),
    (r"(?:maximi[sz]e|full ?screen|make (?:this|it)(?: window)? (?:bigger|full ?screen|full size))(?: this| the)?(?: window| app)?", "maximize"),
    (r"(?:restore|unmaximi[sz]e|make (?:this|it) smaller|normal size)(?: this| the)?(?: window| app)?", "restore"),
    (r"(?:minimi[sz]e)(?: this| the| my| current)?(?: window| app)?", "minimize"),
    (r"(?:minimi[sz]e (?:all|everything)|show (?:the )?desktop|hide all windows|clear (?:the )?desktop)", "desktop"),
    (r"(?:move|send|put|throw)(?: this| the| my)?(?: current)?(?: window| app)? (?:to )?(?:the |my )?(?:(other|second|next|another|external|primary|first|previous|left|right) )(?:monitor|screen|display)", "monitor"),
    (r"(?:switch|go|move|change)(?: to)?(?: the)? (next|previous|left|right) (?:virtual )?desktop|(?:next|previous) (?:virtual )?desktop", "desktopswitch"),
    (r"(?:new|create|add)(?: a)?(?: new)? (?:virtual )?desktop", "newdesktop"),
    (r"(?:task view|show all (?:my )?windows|overview)", "taskview"),
]


def parse(text: str):
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?")
    t = re.sub(r"^(?:please |can you |could you )", "", t)
    for pat, op in _PATTERNS:
        m = re.fullmatch(pat, t)
        if m:
            arg = next((g for g in m.groups() if g), "")
            if op == "desktopswitch":
                arg = arg or ("previous" if "previous" in t else "next")
            return op, arg
    return None


def _keys(*combo):
    def run():
        import pyautogui
        pyautogui.hotkey(*combo)
        return True, ""
    return run


def _set_rect(side: str):
    """Fallback without shortcuts: place the active window on a half of the primary work area."""
    def run():
        if not sys.platform.startswith("win"):
            return False, "not windows"
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = ss.foreground().get("hwnd")
        if not hwnd:
            return False, "no window"
        wa = wintypes.RECT()
        u.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)          # SPI_GETWORKAREA
        w, h = wa.right - wa.left, wa.bottom - wa.top
        x, y, ww, hh = {"left": (wa.left, wa.top, w // 2, h), "right": (wa.left + w // 2, wa.top, w // 2, h),
                        "top": (wa.left, wa.top, w, h // 2), "bottom": (wa.left, wa.top + h // 2, w, h // 2)}[side]
        u.ShowWindow(hwnd, 9)                                            # SW_RESTORE
        u.SetWindowPos(hwnd, None, x, y, ww, hh, 0x0004)
        return True, ""
    return run


def do(op: str, arg: str = "") -> Result:
    fg = ss.foreground()
    who = (fg.get("title") or "the window")
    from friendly import short_name
    name = short_name(who, "")
    if op == "snap":
        ok, _m, _t = verify.attempt(f"snap {name} to the {arg}", [("Windows shortcut", _keys("win", arg)), ("window placement", _set_rect(arg))],
                                    verify.rect_changed(), settle=0.5, timeout=1.5, deep=False)
        return (True, f"Snapped {name} to the {arg}.") if ok else (False, f"{name} didn't move. It may already be there.")
    if op == "snaptb":
        if arg == "top":
            ok, _m, _t = verify.attempt(f"snap {name} to the top", [("window placement", _set_rect("top"))], verify.rect_changed(), deep=False)
        else:
            ok, _m, _t = verify.attempt(f"snap {name} to the bottom", [("window placement", _set_rect("bottom"))], verify.rect_changed(), deep=False)
        return (True, f"Snapped {name} to the {arg}.") if ok else (False, f"{name} didn't move.")
    if op == "maximize":
        ok, _m, _t = verify.attempt(f"maximize {name}", [("Windows shortcut", _keys("win", "up"))], verify.rect_changed(), settle=0.5, deep=False)
        return (True, f"{name} is full size.") if ok else (True, f"{name} already looks maximized.")
    if op == "restore":
        ok, _m, _t = verify.attempt(f"restore {name}", [("Windows shortcut", _keys("win", "down"))], verify.rect_changed(), settle=0.5, deep=False)
        return (True, f"Restored {name}.") if ok else (False, f"{name} didn't change size.")
    if op == "minimize":
        ok, _m, _t = verify.attempt(f"minimize {name}", [("Windows shortcut", _keys("win", "down")), ("Windows shortcut again", _keys("win", "down"))],
                                    verify.foreground_changed(), settle=0.5, deep=False)
        return (True, f"Minimized {name}.") if ok else (False, f"{name} is still on screen.")
    if op == "desktop":
        ok, _m, _t = verify.attempt("show the desktop", [("Windows shortcut", _keys("win", "d"))], verify.screen_changed(0.01), settle=0.6)
        return (True, "Showing the desktop.") if ok else (False, "The desktop didn't appear.")
    if op == "monitor":
        back = arg in {"previous", "first", "left"}
        keys = ("win", "shift", "left" if back else "right")
        ok, _m, _t = verify.attempt(f"move {name} to the other monitor", [("Windows shortcut", _keys(*keys))], verify.rect_changed(), settle=0.7, deep=False)
        return (True, f"Moved {name} to the other screen.") if ok else (False, "It didn't move. Do you have a second monitor connected, Chief?")
    if op == "desktopswitch":
        left = arg in {"previous", "left"}
        ok, _m, _t = verify.attempt(f"go to the {arg} desktop", [("Windows shortcut", _keys("ctrl", "win", "left" if left else "right"))],
                                    verify.screen_changed(0.01), settle=0.8, timeout=2.0)
        return (True, f"On the {arg} desktop.") if ok else (False, "There's no other desktop in that direction.")
    if op == "newdesktop":
        ok, _m, _t = verify.attempt("create a new desktop", [("Windows shortcut", _keys("ctrl", "win", "d"))], verify.screen_changed(0.01), settle=0.8)
        return (True, "New desktop ready.") if ok else (False, "I couldn't create a new desktop.")
    if op == "taskview":
        ok, _m, _t = verify.attempt("open task view", [("Windows shortcut", _keys("win", "tab"))], verify.screen_changed(0.01), settle=0.6)
        return (True, "Here are all your windows.") if ok else (False, "Task view didn't open.")
    return False, "I don't know that window command."


def handle(text: str):
    p = parse(text)
    if not p:
        return None
    return do(*p)