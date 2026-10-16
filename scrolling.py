"""VAANI scrolling: several ways to scroll, and PROOF the page moved (not just "something on screen changed").

A playing video or an animation changes pixels without any scrolling, so success is judged by measuring whether the
screen content slid up or down (vertical_shift). Methods, in order:
  1. browser extension   (finds the inner scrolling area of YouTube / WhatsApp / Gmail; reports whether it moved)
  2. wheel message       (sends the mouse-wheel straight to the browser's page window, no cursor needed)
  3. mouse wheel         (parks the cursor on the active window, then turns the real wheel)
  4. keyboard            (Page Down / Page Up / Home / End)
scroll(direction, extension=None) -> (ok, method_that_worked | reason)
Run `python diagnose.py scroll` to see which method works on your PC.
"""
from __future__ import annotations

import logging
import sys
import time

import numpy as np

log = logging.getLogger("vaani.scroll")
IS_WIN = sys.platform.startswith("win")
SNAP = (192, 108)


# ------------------------------------------------------------------ evidence
def vertical_shift(a: np.ndarray, b: np.ndarray):
    """-> (moved, shift_rows, change). moved=True when b looks like a vertically shifted copy of a."""
    A, B = a.astype(np.float32), b.astype(np.float32)
    h = A.shape[0]
    d0 = float(np.abs(A - B).mean())
    if d0 < 1.5:
        return False, 0, d0                                  # identical: nothing happened
    best_s, best_d = 0, d0
    for s in range(-int(h * 0.7), int(h * 0.7) + 1):
        if abs(s) < 2:
            continue
        d = float(np.abs(A[s:] - B[:h - s]).mean()) if s > 0 else float(np.abs(A[:h + s] - B[-s:]).mean())
        if d < best_d:
            best_s, best_d = s, d
    return (best_s != 0 and best_d < 0.6 * d0 and d0 > 3.0), best_s, d0


def snapshot() -> np.ndarray:
    import pyautogui
    img = pyautogui.screenshot().convert("L").resize(SNAP)
    return np.asarray(img, dtype=np.float32).reshape(SNAP[1], SNAP[0])


# ------------------------------------------------------------------ methods
def _foreground():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return h, (r.left, r.top, r.right, r.bottom)


def _page_hwnd(top):
    """The child window that actually draws a browser page (Chrome / Edge / Brave); falls back to the top window."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _l):
        buf = ctypes.create_unicode_buffer(80)
        u.GetClassNameW(h, buf, 80)
        if buf.value in ("Chrome_RenderWidgetHostHWND", "MozillaWindowClass"):
            found.append(h)
        return True
    u.EnumChildWindows(top, proc(cb), 0)
    return found[0] if found else top


def method_wheel_message(direction: str, notches: int = 5) -> bool:
    if not IS_WIN:
        return False
    import ctypes
    u = ctypes.windll.user32
    top, rect = _foreground()
    if not top:
        return False
    target = _page_hwnd(top)
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2 + 40
    lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    big = direction in {"top", "bottom"}
    delta = 120 * (60 if big else 1) * (1 if direction in {"up", "top"} else -1)
    count = 3 if big else notches
    for _ in range(count):
        u.PostMessageW(target, 0x020A, ((delta & 0xFFFF) << 16), lparam)             # WM_MOUSEWHEEL
        time.sleep(0.02)
    return True


def method_mouse_wheel(direction: str, notches: int = 5) -> bool:
    import pyautogui
    if IS_WIN:
        _top, rect = _foreground()
        if rect[2] - rect[0] > 50:
            pyautogui.moveTo((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2 + 40)     # wheel goes to the window UNDER the cursor
    amount = 120 * notches * (1 if direction in {"up", "top"} else -1)
    if direction in {"top", "bottom"}:
        amount *= 12
    pyautogui.scroll(amount)
    return True


def method_keyboard(direction: str) -> bool:
    import pyautogui
    pyautogui.press({"up": "pageup", "down": "pagedown", "top": "home", "bottom": "end"}[direction])
    return True


# ------------------------------------------------------------------ orchestration
def scroll(direction: str, extension=None, settle: float = 0.35, timeout: float = 1.0):
    """Try each method until the page measurably moved. extension: optional callable -> True/False (the Chrome helper)."""
    d = direction if direction in {"up", "down", "top", "bottom"} else "down"
    methods = []
    if extension is not None:
        methods.append(("extension", lambda: extension(d)))
    methods += [("wheel message", lambda: method_wheel_message(d)), ("mouse wheel", lambda: method_mouse_wheel(d)),
                ("keyboard", lambda: method_keyboard(d))]
    try:
        before = snapshot()
    except Exception as exc:
        return False, f"I can't see the screen to check the scroll: {exc}"
    tried = []
    for name, fn in methods:
        try:
            result = fn()
        except Exception as exc:
            log.info("scroll method %s crashed: %s", name, exc)
            tried.append(f"{name}: error")
            continue
        if name == "extension" and result is True:
            log.info("scroll OK via extension")
            return True, name                                  # the page itself reported that it moved
        if result is False:
            tried.append(f"{name}: unavailable")
            continue
        end = time.monotonic() + timeout
        time.sleep(settle)
        while True:
            after = snapshot()
            moved, shift, change = vertical_shift(before, after)
            if moved or time.monotonic() >= end:
                break
            time.sleep(0.15)
        log.info("scroll via %s: moved=%s shift=%s change=%.1f", name, moved, shift, change)
        tried.append(f"{name}: {'moved' if moved else 'no movement'}")
        if moved:
            return True, name
        before = after
    return False, "; ".join(tried)