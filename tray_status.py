"""VAANI Windows notification-area icon; steady state colors, no floating orb."""
from __future__ import annotations
import logging, threading
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item
from vaani_control import get_status, is_paused, request_pause, request_shutdown, subscribe

log = logging.getLogger("vaani.tray")
_icon = None
_lock = threading.Lock()
_COLORS = {
    "starting": (125, 125, 125), "wake": (45, 170, 80), "command": (230, 160, 35),
    "processing": (55, 130, 220), "speaking": (145, 80, 200), "paused": (115, 115, 115),
    "error": (210, 55, 55), "stopped": (75, 75, 75),
}

def _image(state="starting"):
    im=Image.new("RGBA", (64,64), (0,0,0,0)); d=ImageDraw.Draw(im)
    c=_COLORS.get(state, _COLORS["starting"])
    d.ellipse((5,5,59,59), fill=c, outline=(245,245,245,255), width=2)
    # Simple white microphone glyph
    d.rounded_rectangle((26,15,38,37), radius=6, fill="white")
    d.arc((19,23,45,45), start=0, end=180, fill="white", width=3)
    d.line((32,43,32,49), fill="white", width=3); d.line((25,50,39,50), fill="white", width=3)
    return im

def _tooltip():
    state, detail = get_status()
    return ("VAANI — " + (detail or state)).replace("\n", " ")[:127]

def _refresh(state=None, detail=None):
    global _icon
    with _lock:
        icon=_icon
    if icon is None: return
    try:
        icon.icon=_image(state or get_status()[0]); icon.title=_tooltip()
        icon.update_menu()
    except Exception:
        log.exception("Could not refresh tray icon")

def _toggle_pause(icon, item):
    request_pause(not is_paused()); _refresh()

def _show_status(icon, item):
    state, detail=get_status()
    try:
        from tkinter import Tk, messagebox
        root=Tk(); root.withdraw(); messagebox.showinfo("VAANI status", f"State: {state}\n{detail or ''}", parent=root); root.destroy()
    except Exception:
        log.exception("Could not show status dialog")

def _exit(icon, item):
    request_shutdown()
    icon.stop()

def start_tray() -> bool:
    global _icon
    with _lock:
        if _icon is not None: return True
        state, _ = get_status()
        menu=pystray.Menu(
            Item(lambda text: "Resume microphone" if is_paused() else "Pause microphone", _toggle_pause),
            Item("Show status", _show_status),
            Item("Exit VAANI", _exit),
        )
        _icon=pystray.Icon("VAANI", _image(state), _tooltip(), menu)
        subscribe(_refresh)
        _icon.run_detached()
    return True

def stop_tray() -> None:
    global _icon
    with _lock: icon, _icon = _icon, None
    if icon is not None:
        try: icon.stop()
        except Exception: log.exception("Could not stop tray icon")
