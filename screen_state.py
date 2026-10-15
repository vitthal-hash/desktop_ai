"""VAANI screen state: what is REALLY on the screen right now, and did something change?

foreground()            -> {"title","hwnd","rect"}            the active window
list_windows()          -> [titles]                           visible top-level windows
elements(max_n)         -> [Element dicts]                    real clickable controls of the active window (UI Automation)
describe_elements(els)  -> text block for a language model    "[12] Button 'Save' (640,380)"
annotated_png(els)      -> PNG bytes with numbered boxes on the screenshot (Set-of-Marks, for vision models)
fingerprint()           -> dict snapshot used to prove an action had an effect
diff(before, after)     -> (changed, [reasons])

Everything degrades gracefully: on a PC without UI Automation the element list is empty and the screenshot
difference is used instead.
"""
from __future__ import annotations

import io
import logging
import sys
import time

log = logging.getLogger("vaani.state")
IS_WIN = sys.platform.startswith("win")

_INTERACTIVE = {"ButtonControl", "EditControl", "MenuItemControl", "ListItemControl", "TabItemControl", "CheckBoxControl",
                "RadioButtonControl", "HyperlinkControl", "ComboBoxControl", "SplitButtonControl", "TreeItemControl",
                "DataItemControl", "SliderControl", "ToggleButtonControl", "DocumentControl"}
_LABELLED = {"TextControl"}                 # not clickable, but their text tells the model what is on screen


# ------------------------------------------------------------------ windows
def foreground() -> dict:
    if not IS_WIN:
        return {"title": "", "hwnd": 0, "rect": (0, 0, 0, 0)}
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        return {"title": buf.value, "hwnd": int(h or 0), "rect": (r.left, r.top, r.right, r.bottom)}
    except Exception:
        return {"title": "", "hwnd": 0, "rect": (0, 0, 0, 0)}


def list_windows() -> list:
    try:
        import pygetwindow
        return sorted({(w.title or "").strip() for w in pygetwindow.getAllWindows() if (w.title or "").strip() and w.visible})
    except Exception:
        return []


# ----------------------------------------------------------------- elements
def elements(max_n: int = 70, max_depth: int = 22) -> list:
    """Numbered, on-screen, enabled controls of the active window in reading order (top->bottom, left->right)."""
    if not IS_WIN:
        return []
    try:
        import uiautomation as auto
        win = auto.GetForegroundControl()
        wr = win.BoundingRectangle
    except Exception as exc:
        log.info("UIA unavailable: %s", exc)
        return []
    found, seen = [], set()
    try:
        for c, _depth in auto.WalkControl(win, includeTop=False, maxDepth=max_depth):
            try:
                kind = c.ControlTypeName
                if kind not in _INTERACTIVE and kind not in _LABELLED:
                    continue
                if c.IsOffscreen:
                    continue
                r = c.BoundingRectangle
                if r.width() < 6 or r.height() < 6:
                    continue
                if r.right < wr.left or r.left > wr.right or r.bottom < wr.top or r.top > wr.bottom:
                    continue
                name = (c.Name or "").strip().replace("\n", " ")[:70]
                if kind in _LABELLED and (not name or len(name) < 2):
                    continue
                if kind not in _LABELLED and not name and kind not in {"EditControl", "ComboBoxControl", "DocumentControl"}:
                    name = (c.AutomationId or c.ClassName or "")[:40]
                key = (kind, name, r.left // 4, r.top // 4)
                if key in seen:
                    continue
                seen.add(key)
                found.append({"type": kind.replace("Control", ""), "name": name, "id": c.AutomationId or "",
                              "rect": (r.left, r.top, r.right, r.bottom), "enabled": bool(c.IsEnabled),
                              "clickable": kind in _INTERACTIVE, "_ctl": c})
            except Exception:
                continue
            if len(found) >= max_n * 3:
                break
    except Exception as exc:
        log.info("UIA walk failed: %s", exc)
    found.sort(key=lambda e: (e["rect"][1] // 25, e["rect"][0]))
    # keep every clickable element first, then fill up with labels
    clickable = [e for e in found if e["clickable"]]
    labels = [e for e in found if not e["clickable"]]
    picked = (clickable[:max_n] + labels[: max(0, max_n - len(clickable))])
    picked.sort(key=lambda e: (e["rect"][1] // 25, e["rect"][0]))
    for i, e in enumerate(picked, 1):
        e["n"] = i
    return picked


def center(e: dict) -> tuple:
    l, t, r, b = e["rect"]
    return (l + r) // 2, (t + b) // 2


def describe_elements(els: list) -> str:
    lines = []
    for e in els:
        cx, cy = center(e)
        flag = "" if e.get("enabled", True) else " (disabled)"
        what = "text" if not e.get("clickable") else e["type"]
        lines.append(f"[{e['n']}] {what} '{e['name']}' at ({cx},{cy}){flag}")
    return "\n".join(lines)


def screenshot():
    import pyautogui
    return pyautogui.screenshot().convert("RGB")


def annotated_png(els: list, max_side: int = 1600) -> bytes:
    """Screenshot with a numbered box around every listed element, so a vision model can say 'element 12'."""
    from PIL import ImageDraw
    img = screenshot()
    d = ImageDraw.Draw(img)
    for e in els:
        if not e.get("clickable"):
            continue
        l, t, r, b = e["rect"]
        d.rectangle([l, t, r, b], outline=(255, 40, 40), width=2)
        tag = str(e["n"])
        d.rectangle([l, t, l + 8 * len(tag) + 6, t + 14], fill=(255, 40, 40))
        d.text((l + 3, t + 1), tag, fill=(255, 255, 255))
    if max(img.size) > max_side:
        s = max_side / max(img.size)
        img = img.resize((int(img.size[0] * s), int(img.size[1] * s)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------- fingerprint
def _small_gray():
    try:
        return screenshot().convert("L").resize((96, 54))
    except Exception:
        return None


def fingerprint(deep: bool = True) -> dict:
    fp = {"t": time.monotonic(), "fg": foreground(), "windows": list_windows(), "pixels": None, "ui": None}
    if deep:
        img = _small_gray()
        fp["pixels"] = list(img.getdata()) if img is not None else None
        try:
            els = elements(40)
            fp["ui"] = hash(tuple((e["type"], e["name"], e["rect"][0] // 6, e["rect"][1] // 6) for e in els))
        except Exception:
            fp["ui"] = None
    return fp


def pixel_change(a, b) -> float:
    """Fraction (0-1) of screen cells that changed noticeably."""
    if not a or not b or len(a) != len(b):
        return 0.0
    changed = sum(1 for x, y in zip(a, b) if abs(x - y) > 14)
    return changed / len(a)


def diff(before: dict, after: dict, min_pixels: float = 0.004) -> tuple:
    """(changed, reasons) between two fingerprints."""
    reasons = []
    if before["fg"].get("hwnd") != after["fg"].get("hwnd"):
        reasons.append("active window changed")
    elif before["fg"].get("title") != after["fg"].get("title"):
        reasons.append("window title changed")
    if before["fg"].get("rect") != after["fg"].get("rect"):
        reasons.append("window moved or resized")
    if set(before["windows"]) != set(after["windows"]):
        reasons.append("windows opened or closed")
    if before.get("ui") is not None and after.get("ui") is not None and before["ui"] != after["ui"]:
        reasons.append("on-screen controls changed")
    pc = pixel_change(before.get("pixels"), after.get("pixels"))
    if pc >= min_pixels:
        reasons.append(f"screen changed ({pc * 100:.1f}%)")
    return bool(reasons), reasons