"""
VAANI pc_control: reliable "hands" for Windows.  Drop next to tools.py / main.py.

Order of attack for every command (most reliable first):
  1. Deterministic handlers  (close/open/focus apps, media session, WhatsApp, new Colab/Docs...)
  2. Windows UI Automation   (reads real buttons/list items by name, clicks them like a mouse)
  3. Vision                  (screenshot -> model finds the thing -> click)   [Gemini key or local Ollama VLM]
  4. Multi-step planner      ("do A then B", "after opening X, do Y")  with verification between steps

Entry points used by main.py:   handle(text)  and  rescue(text, result)
Every function returns (ok: bool, spoken_message: str) and never raises.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import time
from typing import Callable, Optional, Tuple

import env_loader  # noqa: F401  (loads .env)

Result = Tuple[bool, str]
log = logging.getLogger("vaani.pc")

try:  # make screenshot pixels == click pixels on 125%/150% scaled laptop screens (big click-accuracy fix)
    import ctypes as _ct
    _ct.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

AUTO_SEND = os.getenv("VAANI_AUTO_SEND", "0") == "1"      # 1 = send WhatsApp msgs without asking "send it?"
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")

# ------------------------------------------------------------------ small helpers
_pending: Optional[dict] = None


def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1
    return pyautogui


def _clip_set(text: str) -> None:
    import tkinter as tk
    r = tk.Tk(); r.withdraw(); r.clipboard_clear(); r.clipboard_append(text); r.update(); r.destroy()


def _paste(text: str) -> None:
    _clip_set(text)
    time.sleep(0.05)
    _pg().hotkey("ctrl", "v")


def _clean(text: str) -> str:
    t = " ".join(text.lower().split()).strip(" .!?,")
    t = re.sub(r"^(?:hey computer|vaani)[\s,]*", "", t)
    t = re.sub(r"^(?:please|can you|could you|would you|will you|i want you to|i need you to|go ahead and)\s+", "", t)
    t = re.sub(r"\s+(?:please|for me)$", "", t)
    return t.strip()


# ------------------------------------------------------------------ windows / processes
def _windows():
    """[(hwnd, title, pid, exe)] for visible top-level windows with a title."""
    import ctypes
    from ctypes import wintypes
    import psutil
    u32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    exe = psutil.Process(pid.value).name().lower()
                except Exception:
                    exe = ""
                out.append((hwnd, buf.value, pid.value, exe))
        return True

    u32.EnumWindows(cb, 0)
    me = os.getpid()
    return [w for w in out if w[2] != me and w[1] not in {"Program Manager", "Windows Input Experience"}]


# spoken name -> (process names, title hints, launch command)
APPS = {
    "notepad": (["notepad.exe"], ["notepad"], "notepad"),
    "calculator": (["calculatorapp.exe", "calc.exe"], ["calculator"], "calc"),
    "paint": (["mspaint.exe"], ["paint"], "mspaint"),
    "file explorer": (["explorer.exe"], ["file explorer"], "explorer"),
    "explorer": (["explorer.exe"], ["file explorer"], "explorer"),
    "command prompt": (["cmd.exe"], ["command prompt"], "cmd"),
    "cmd": (["cmd.exe"], ["command prompt"], "cmd"),
    "powershell": (["powershell.exe"], ["powershell"], "powershell"),
    "terminal": (["windowsterminal.exe"], ["terminal"], "wt"),
    "task manager": (["taskmgr.exe"], ["task manager"], "taskmgr"),
    "chrome": (["chrome.exe"], ["google chrome"], "chrome"),
    "google chrome": (["chrome.exe"], ["google chrome"], "chrome"),
    "edge": (["msedge.exe"], ["edge"], "msedge"),
    "vs code": (["code.exe"], ["visual studio code"], "code"),
    "vscode": (["code.exe"], ["visual studio code"], "code"),
    "visual studio code": (["code.exe"], ["visual studio code"], "code"),
    "word": (["winword.exe"], ["word"], "winword"),
    "excel": (["excel.exe"], ["excel"], "excel"),
    "powerpoint": (["powerpnt.exe"], ["powerpoint"], "powerpnt"),
    "whatsapp": (["whatsapp.exe", "whatsapp.root.exe"], ["whatsapp"], "whatsapp:"),
    "spotify": (["spotify.exe"], ["spotify"], "spotify:"),
    "telegram": (["telegram.exe"], ["telegram"], "telegram"),
    "discord": (["discord.exe"], ["discord"], "discord"),
    "settings": (["systemsettings.exe"], ["settings"], "ms-settings:"),
    "camera": (["windowscamera.exe"], ["camera"], "microsoft.windows.camera:"),
    "vlc": (["vlc.exe"], ["vlc"], "vlc"),
    "zoom": (["zoom.exe"], ["zoom"], "zoom"),
    "teams": (["ms-teams.exe", "teams.exe"], ["teams"], "msteams:"),
}


def _match_windows(name: str):
    name = name.lower().strip()
    procs, hints, _ = APPS.get(name, ([], [name], None))
    hits = []
    for hwnd, title, pid, exe in _windows():
        t = title.lower()
        if exe in procs or any(h in t for h in hints):
            hits.append((hwnd, title, pid, exe))
    return hits


def _force_foreground(hwnd) -> bool:
    import ctypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)
    u32.keybd_event(0x12, 0, 0, 0); u32.keybd_event(0x12, 0, 2, 0)  # tap Alt so SetForegroundWindow is allowed
    cur = k32.GetCurrentThreadId()
    fg = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    u32.AttachThreadInput(cur, fg, True)
    try:
        u32.BringWindowToTop(hwnd); u32.SetForegroundWindow(hwnd)
    finally:
        u32.AttachThreadInput(cur, fg, False)
    return u32.GetForegroundWindow() == hwnd


def focus_app(name: str) -> Result:
    try:
        hits = _match_windows(name)
        if not hits:
            return False, f"I don't see {name} open."
        return (True, f"Switched to {name}.") if _force_foreground(hits[0][0]) else (False, f"Windows wouldn't let me bring {name} forward.")
    except Exception as e:
        return False, f"Couldn't switch to {name}: {e}"


def close_app(name: str, force: bool = False) -> Result:
    """Politely close every window of the app (WM_CLOSE), verify it, optionally force-kill."""
    try:
        import ctypes
        hits = _match_windows(name)
        if not hits:
            return False, f"I don't see {name} open."
        for hwnd, *_ in hits:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(1.5)
        left = _match_windows(name)
        if not left:
            return True, f"Closed {name}."
        if force:
            exes = {e for _, _, _, e in left if e and e not in {"applicationframehost.exe", "explorer.exe"}}
            for exe in exes:
                subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, creationflags=0x08000000)
            time.sleep(1)
            return (True, f"Force closed {name}.") if not _match_windows(name) else (False, f"{name} is still open.")
        return False, f"{name} is still open, probably asking whether to save. Say 'force close {name}' to kill it without saving."
    except Exception as e:
        return False, f"Couldn't close {name}: {e}"


def open_app(name: str) -> Result:
    """Focus if running, else launch by command, verify a window appeared, else fall back to Start search."""
    key = name.lower().strip()
    try:
        if _match_windows(key):
            return focus_app(key)
        cmd = APPS.get(key, (None, None, None))[2]
        if cmd:
            try:
                if cmd.endswith(":"):
                    os.startfile(cmd)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["cmd", "/c", "start", "", cmd], creationflags=0x08000000)
            except Exception:
                cmd = None
            for _ in range(16):
                time.sleep(0.5)
                hits = _match_windows(key)
                if hits:
                    try:
                        _force_foreground(hits[0][0])
                    except Exception:
                        pass
                    return True, f"Opened {name}."
        if resolve_start_app(key):  # any installed app incl. Microsoft Store apps
            ok, msg = launch_start_app(key)
            if ok:
                return ok, msg
        # Still unknown: Start-menu search, then verify by window title
        pg = _pg()
        pg.press("win"); time.sleep(0.5); pg.write(key[:60], interval=0.03); time.sleep(0.9); pg.press("enter")
        for _ in range(14):
            time.sleep(0.5)
            if _match_windows(key):
                return True, f"Opened {name}."
        return False, f"I searched for {name} but no window appeared. It may not be installed."
    except Exception as e:
        return False, f"Couldn't open {name}: {e}"


# ------------------------------------------------------------------ media (real playback session, not blind keys)
def _gsmtc(op: str) -> Optional[bool]:
    """Control the actual Windows media session (Spotify, Chrome/YouTube, VLC...). None = unavailable."""
    try:
        import asyncio
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

        async def run():
            s = (await M.request_async()).get_current_session()
            if s is None:
                return None
            playing = int(s.get_playback_info().playback_status) == 4
            if op == "pause":
                return True if not playing else await s.try_pause_async()
            if op == "play":
                return True if playing else await s.try_play_async()
            if op == "next":
                return await s.try_skip_next_async()
            if op == "previous":
                return await s.try_skip_previous_async()
            return None
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run())
        finally:
            loop.close()
    except Exception as e:
        log.info("gsmtc unavailable: %s", e)
        return None


def media(op: str) -> Result:
    labels = {"pause": "Paused.", "play": "Playing.", "next": "Next track.", "previous": "Previous track."}
    r = _gsmtc(op)
    if r:
        return True, labels[op]
    try:  # fallback: hardware media key (toggle; cannot know current state)
        key = {"pause": "playpause", "play": "playpause", "next": "nexttrack", "previous": "prevtrack"}[op]
        _pg().press(key)
        return True, labels[op] + " (sent a media key; I couldn't read the player state)"
    except Exception as e:
        return False, f"Media control failed: {e}"


# ------------------------------------------------------------------ UI Automation (like a mouse, but exact)
def _uia():
    import uiautomation as auto
    return auto


def _fg_window():
    auto = _uia()
    return auto.GetForegroundControl()


def uia_click(label: str, kinds: tuple = (), double: bool = False) -> Result:
    """Find a control whose name contains `label` in the focused window and click its centre."""
    try:
        auto = _uia()
        win = _fg_window()
        want = label.lower()
        best = None
        for c, _ in auto.WalkControl(win, includeTop=False, maxDepth=25):
            try:
                nm = (c.Name or "").lower()
                if not nm or want not in nm:
                    continue
                if kinds and c.ControlTypeName not in kinds:
                    continue
                r = c.BoundingRectangle
                if r.width() <= 0 or r.height() <= 0:
                    continue
                score = (nm != want, len(nm))  # exact match first, then shortest name
                if best is None or score < best[0]:
                    best = (score, c)
            except Exception:
                continue
        if not best:
            return False, f"I can't find '{label}' in the active window."
        c = best[1]
        c.DoubleClick() if double else c.Click()
        return True, f"Clicked {c.Name[:60]}."
    except Exception as e:
        return False, f"UI automation failed: {e}"


def uia_nth_item(n: int, left_only: bool = True) -> Result:
    """Click the n-th list item (top to bottom) in the active window; left_only = chat/sidebar list."""
    try:
        auto = _uia()
        win = _fg_window()
        wr = win.BoundingRectangle
        mid = wr.left + (wr.right - wr.left) * 0.45
        items = []
        for c, _ in auto.WalkControl(win, includeTop=False, maxDepth=25):
            try:
                if c.ControlTypeName not in ("ListItemControl", "DataItemControl"):
                    continue
                r = c.BoundingRectangle
                if r.width() < 120 or r.height() < 30 or (left_only and r.right > mid):
                    continue
                items.append((r.top, c))
            except Exception:
                continue
        items.sort(key=lambda t: t[0])
        if len(items) < n:
            return False, f"I only found {len(items)} items there."
        items[n - 1][1].Click()
        return True, f"Opened item {n}."
    except Exception as e:
        return False, f"UI automation failed: {e}"


def uia_focus_edit(label_hint: str = "") -> bool:
    try:
        auto = _uia()
        for c, _ in auto.WalkControl(_fg_window(), includeTop=False, maxDepth=25):
            if c.ControlTypeName == "EditControl" and (not label_hint or label_hint in (c.Name or "").lower()):
                c.Click()
                return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------ vision (the "see and click" layer)
def _shot(max_side: int = 1400):
    img = _pg().screenshot().convert("RGB")
    full = img.size
    if max(full) > max_side:
        s = max_side / max(full)
        img = img.resize((int(full[0] * s), int(full[1] * s)))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue(), full, img.size


def _ollama_locate(description: str):
    """Local VLM grounding (qwen2.5vl). Returns (x, y) in real screen pixels or None."""
    import ollama
    png, full, sent = _shot()
    prompt = (f'Locate "{description}" in this screenshot. Reply with JSON only: '
              '{"found": true, "bbox_2d": [x1, y1, x2, y2]} in pixel coordinates of this image, or {"found": false}.')
    r = ollama.chat(model=OLLAMA_VISION_MODEL, format="json", options={"temperature": 0},
                    messages=[{"role": "user", "content": prompt, "images": [base64.b64encode(png).decode()]}])
    d = json.loads(r.message.content or "{}")
    if isinstance(d, list) and d:
        d = d[0]
    box = d.get("bbox_2d") if isinstance(d, dict) and d.get("found") else None
    if not box or len(box) != 4:
        return None
    x1, y1, x2, y2 = [float(v) for v in box]
    return int((x1 + x2) / 2 * full[0] / sent[0]), int((y1 + y2) / 2 * full[1] / sent[1])


def vision_click(description: str, double: bool = False, right: bool = False) -> Result:
    if os.getenv("GEMINI_API_KEY", "").strip():
        try:
            import screen_vision
            ok, msg = screen_vision.click_described(description, double=double, right=right)
            if ok:
                return ok, msg
        except Exception as e:
            log.info("gemini vision failed: %s", e)
    try:
        pos = _ollama_locate(description)
        if not pos:
            return False, f"I can't see '{description}' on the screen."
        pg = _pg()
        (pg.rightClick if right else pg.doubleClick if double else pg.click)(*pos)
        return True, f"Clicked {description[:60]}."
    except Exception as e:
        return False, f"Vision click failed: {e}"


def smart_click(description: str, double: bool = False, right: bool = False) -> Result:
    """UI Automation -> Chrome page text -> vision."""
    if not right:
        ok, msg = uia_click(description, double=double)
        if ok:
            return ok, msg
    try:
        import tools
        if tools._bridge_ready() and tools._chrome_in_front():
            ok, msg = tools._bridge_result("click_text", text=description)
            if ok:
                return ok, msg
    except Exception:
        pass
    if not right:
        ok, msg = ocr_click(description, double=double)
        if ok:
            return ok, msg
    return vision_click(description, double=double, right=right)


def vision_yes_no(question: str) -> Optional[bool]:
    """Ask the vision model a yes/no question about the screen. None = can't tell."""
    try:
        png, _, _ = _shot()
        if os.getenv("GEMINI_API_KEY", "").strip():
            import screen_vision
            txt, err = screen_vision._ask(question + " Answer with only the word yes or no.", png, want_json=False)
            if not err:
                return txt.strip().lower().startswith("yes")
        import ollama
        r = ollama.chat(model=OLLAMA_VISION_MODEL, options={"temperature": 0},
                        messages=[{"role": "user", "content": question + " Answer with only yes or no.",
                                   "images": [base64.b64encode(png).decode()]}])
        return (r.message.content or "").strip().lower().startswith("yes")
    except Exception:
        return None



# ------------------------------------------------------------------ NEW: Start-menu app resolver (finds ANY installed app incl. Store apps)
_start_cache: Optional[list] = None


def _start_apps() -> list:
    global _start_cache
    if _start_cache is None:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
                                 capture_output=True, text=True, timeout=30, creationflags=0x08000000).stdout
            data = json.loads(out or "[]")
            data = [data] if isinstance(data, dict) else data
            _start_cache = [(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]
        except Exception as e:
            log.info("Get-StartApps failed: %s", e)
            _start_cache = []
    return _start_cache


def resolve_start_app(name: str, cutoff: float = 0.78):
    import difflib
    name = name.lower().strip()
    apps = _start_apps()
    if not apps or not name:
        return None
    names = {n.lower(): (n, a) for n, a in apps}
    if name in names:
        return names[name]
    sub = [v for k, v in names.items() if name in k]
    if sub:
        return min(sub, key=lambda v: len(v[0]))
    close = difflib.get_close_matches(name, list(names), n=1, cutoff=cutoff)
    return names[close[0]] if close else None


def launch_start_app(name: str) -> Result:
    hit = resolve_start_app(name)
    if not hit:
        return False, f"I couldn't find an installed app called {name}."
    try:
        os.startfile("shell:AppsFolder\\" + hit[1])  # type: ignore[attr-defined]
        key = hit[0].lower()
        for _ in range(20):
            time.sleep(0.5)
            w = [x for x in _windows() if key.split()[0] in x[1].lower() or key.split()[0] in x[3]]
            if w:
                try:
                    _force_foreground(w[0][0])
                except Exception:
                    pass
                return True, f"Opened {hit[0]}."
        return False, f"I started {hit[0]} but no window appeared yet."
    except Exception as e:
        return False, f"Couldn't open {hit[0]}: {e}"


# ------------------------------------------------------------------ NEW: local OCR (Windows built-in, no cloud) = exact text clicking
def _ocr_words():
    """[(text, x, y, w, h)] in screen pixels via the Windows OCR engine (needs winsdk)."""
    import asyncio
    import tempfile
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile
    path = os.path.join(tempfile.gettempdir(), "vaani_ocr.png")
    _pg().screenshot().convert("RGB").save(path)

    async def run():
        f = await StorageFile.get_file_from_path_async(path)
        stream = await f.open_async(FileAccessMode.READ)
        bmp = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
        eng = OcrEngine.try_create_from_user_profile_languages()
        res = await eng.recognize_async(bmp)
        return [(w.text, w.bounding_rect.x, w.bounding_rect.y, w.bounding_rect.width, w.bounding_rect.height)
                for line in res.lines for w in line.words]
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


def ocr_find(label: str, region: Optional[tuple] = None):
    """Centre (x, y) of the best on-screen match for `label` (multi-word, typo tolerant) or None.
    region = (left, top, right, bottom) limits the search."""
    import difflib
    words = _ocr_words()
    norm = lambda t: re.sub(r"[^\w]", "", t.lower())
    toks = [norm(t) for t in label.split() if norm(t)]
    if not toks:
        return None
    best = None
    for i in range(len(words)):
        seq = words[i:i + len(toks)]
        if len(seq) < len(toks):
            break
        score = sum(difflib.SequenceMatcher(None, norm(w[0]), t).ratio() for w, t in zip(seq, toks)) / len(toks)
        if score < 0.8:
            continue
        x1, y1 = min(w[1] for w in seq), min(w[2] for w in seq)
        x2, y2 = max(w[1] + w[3] for w in seq), max(w[2] + w[4] for w in seq)
        cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
        if region and not (region[0] <= cx <= region[2] and region[1] <= cy <= region[3]):
            continue
        if best is None or score > best[0]:
            best = (score, cx, cy)
    return (best[1], best[2]) if best else None


def ocr_click(label: str, double: bool = False) -> Result:
    try:
        pos = ocr_find(label)
        if not pos:
            return False, f"I can't read '{label}' on the screen."
        pg = _pg()
        (pg.doubleClick if double else pg.click)(*pos)
        return True, f"Clicked {label[:60]}."
    except Exception as e:
        return False, f"Screen reading failed: {e}"


def _fg_rect():
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _chat_header_ok(contact: str) -> Optional[bool]:
    """True/False if OCR can/can't see the contact name in the top part of the chat pane; None = OCR unavailable."""
    try:
        l, t, r, b = _fg_rect()
        region = (l + (r - l) * 0.28, t, r, t + (b - t) * 0.22)
        return ocr_find(contact, region) is not None
    except Exception:
        return None


# ------------------------------------------------------------------ NEW: everyday extras
def _speak_later(text: str) -> None:
    try:
        from voice import speak
        speak(text)
    except Exception:
        pass


def aborted() -> bool:
    """Hold Esc to stop a running multi-step task."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)
    except Exception:
        return False


def list_windows() -> Result:
    titles = []
    for _, t, _, exe in _windows():
        if t not in titles:
            titles.append(t)
    return (True, "Open windows: " + "; ".join(x[:40] for x in titles[:8]) + ".") if titles else (False, "I see no open windows.")


def system_status(kind: str) -> Result:
    try:
        import psutil
        if "battery" in kind:
            b = psutil.sensors_battery()
            return (True, f"Battery is at {int(b.percent)} percent" + (", charging." if b.power_plugged else ".")) if b else (False, "No battery found.")
        if "cpu" in kind:
            return True, f"CPU is at {int(psutil.cpu_percent(interval=0.6))} percent."
        m = psutil.virtual_memory()
        return True, f"Memory is {int(m.percent)} percent used, {m.available / 1e9:.1f} gigabytes free."
    except Exception as e:
        return False, f"Couldn't read system status: {e}"


def take_note(text: str) -> Result:
    try:
        from pathlib import Path
        import datetime
        p = Path.home() / "Documents" / "VAANI_Notes.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"- {datetime.datetime.now():%Y-%m-%d %H:%M}  {text}\n")
        return True, "Noted."
    except Exception as e:
        return False, f"Couldn't save the note: {e}"


def read_notes() -> Result:
    try:
        from pathlib import Path
        p = Path.home() / "Documents" / "VAANI_Notes.md"
        lines = [l.strip("- \n") for l in p.read_text(encoding="utf-8").splitlines() if l.strip()][-3:]
        return (True, "Your latest notes: " + " ... ".join(lines)) if lines else (False, "You have no notes yet.")
    except Exception:
        return False, "You have no notes yet."


def set_timer(n: int, unit: str) -> Result:
    import threading
    secs = n * (3600 if unit.startswith("hour") else 60 if unit.startswith("min") else 1)
    threading.Timer(secs, lambda: _speak_later(f"Chief, your {n} {unit} timer is done.")).start()
    return True, f"Timer set for {n} {unit}."


def read_clipboard() -> Result:
    try:
        import tkinter as tk
        r = tk.Tk(); r.withdraw(); v = r.clipboard_get(); r.destroy()
        return True, ("Your clipboard says: " + v[:300]) if v.strip() else "Your clipboard is empty."
    except Exception:
        return False, "Your clipboard is empty or isn't text."


def open_workspace() -> Result:
    apps = [a.strip() for a in os.getenv("VAANI_WORKSPACE", "").split(",") if a.strip()]
    if not apps:
        return False, "Set VAANI_WORKSPACE in your .env first, for example: code,chrome,spotify."
    ok_all, notes = True, []
    for a in apps:
        ok, _ = open_app(a)
        ok_all &= ok
        notes.append(a + ("" if ok else " (failed)"))
        time.sleep(1)
    return ok_all, "Workspace: " + ", ".join(notes) + "."


def describe_screen(question: str = "") -> Result:
    if os.getenv("GEMINI_API_KEY", "").strip():
        try:
            import screen_vision
            return screen_vision.describe(question)
        except Exception:
            pass
    try:
        import ollama
        png, _, _ = _shot()
        r = ollama.chat(model=OLLAMA_VISION_MODEL, options={"temperature": 0.2}, messages=[{
            "role": "user", "content": (question or "Describe what is on this screen") + ". Answer in at most three short sentences.",
            "images": [base64.b64encode(png).decode()]}])
        return True, (r.message.content or "").strip() or "I couldn't describe the screen."
    except Exception as e:
        return False, f"Screen description needs a vision model (ollama pull {OLLAMA_VISION_MODEL}): {e}"


# ------------------------------------------------------------------ WhatsApp
def _ensure_whatsapp() -> Result:
    if _match_windows("whatsapp"):
        return focus_app("whatsapp")
    try:
        os.startfile("whatsapp:")  # type: ignore[attr-defined]
        for _ in range(20):
            time.sleep(0.5)
            if _match_windows("whatsapp"):
                time.sleep(1.5)
                return focus_app("whatsapp")
    except Exception:
        pass
    try:  # desktop app not installed -> WhatsApp Web through the existing browser bridge
        import tools
        ok, msg = tools._navigate_existing_browser("https://web.whatsapp.com", "WhatsApp Web")
        time.sleep(4)
        return ok, msg
    except Exception as e:
        return False, f"I couldn't open WhatsApp: {e}"


def whatsapp_open_chat(contact: str = "", nth: int = 0) -> Result:
    ok, msg = _ensure_whatsapp()
    if not ok:
        return ok, msg
    pg = _pg()
    time.sleep(0.6)
    if nth:
        ok, msg = uia_nth_item(nth)
        if not ok:
            ok, msg = vision_click(f"the number {nth} chat from the top in the chat list on the left side")
        return (ok, f"Opened chat number {nth}." if ok else msg)
    pg.hotkey("ctrl", "f"); time.sleep(0.4)
    pg.hotkey("ctrl", "a"); _paste(contact); time.sleep(1.4)
    pg.press("down"); time.sleep(0.2); pg.press("enter"); time.sleep(0.9)
    seen = _chat_header_ok(contact)
    if seen is None:
        seen = vision_yes_no(f"Is a WhatsApp chat with '{contact}' open (their name shown at the top of the chat)?")
    if seen is False:
        ok, msg = vision_click(f"the chat named {contact} in the search results or chat list")
        if not ok:
            return False, f"I searched for {contact} but couldn't confirm that chat opened."
    return True, f"Opened the chat with {contact}."


def whatsapp_message(contact: str, text: str) -> Result:
    global _pending
    ok, msg = whatsapp_open_chat(contact)
    if not ok:
        return ok, msg
    if not uia_focus_edit("type a message"):
        vision_click("the message input box at the bottom of the chat")
    time.sleep(0.2)
    _paste(text)
    if AUTO_SEND:
        _pg().press("enter")
        return True, f"Sent '{text[:60]}' to {contact}."
    _pending = {"kind": "send", "contact": contact}
    return True, f"I typed '{text[:80]}' in the chat with {contact}. Say send it to send, or cancel."


# ------------------------------------------------------------------ command parsing
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        "ninth": 9, "tenth": 10, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6, "7th": 7, "8th": 8,
        "9th": 9, "10th": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "top": 1, "last": 0}
_ORD_RE = "|".join(sorted(_ORD, key=len, reverse=True)) + r"|\d{1,2}"
_VERBS = r"open|close|type|write|send|search|play|pause|click|go|scroll|press|new|create|switch|minimize|maximize|take|message|text|whatsapp|select|start"


def split_steps(text: str) -> list[str]:
    t = _clean(text)
    m = re.match(r"^after (?:opening|open) (.+?)(?:,\s*(?:then |and then )?|\s+(?:then|and then)\s+)(.+)$", t)
    if m:
        return [f"open {m.group(1)}"] + split_steps(m.group(2))
    parts = re.split(rf"\s*,?\s*(?:and then|after that|afterwards|then)\s+|\s+and\s+(?=(?:{_VERBS})\b)", t)
    return [p.strip() for p in parts if p and p.strip()]


NEW_DOCS = {"notebook": "https://colab.new", "colab notebook": "https://colab.new",
            "document": "https://docs.new", "doc": "https://docs.new", "google doc": "https://docs.new",
            "sheet": "https://sheets.new", "spreadsheet": "https://sheets.new", "slides": "https://slides.new",
            "presentation": "https://slides.new", "form": "https://forms.new"}


def _ord(tok: str) -> int:
    return _ORD.get(tok, int(tok) if tok.isdigit() else 1)


def _single(step: str) -> Optional[Result]:
    """One atomic command. None = not mine, let the rest of VAANI handle it."""
    global _pending
    t = _clean(step)
    if not t:
        return None

    if _pending:
        if t in {"send it", "send", "yes send it", "confirm", "confirm send", "yes", "do it", "confirm action"}:
            p, _pending = _pending, None
            if p["kind"] == "send":
                _pg().press("enter")
                return True, f"Sent your message to {p['contact']}."
            if p["kind"] == "agent":
                import screen_vision
                return screen_vision.screen_task(p["goal"], max_steps=10)
        if t in {"cancel", "no", "don't send", "do not send", "cancel send", "stop"}:
            _pending = None
            return True, "Cancelled. Nothing was sent."

    m = re.fullmatch(r"(force close|kill|force quit|terminate) (?:the )?(.+?)(?: app| application| window)?", t)
    if m and m.group(2) not in {"tab", "this", "it"}:
        return close_app(m.group(2), force=True)
    m = re.fullmatch(r"(?:close|quit|exit|shut down|shutdown) (?:the )?(.+?)(?: app| application| window| program)?", t)
    if m:
        who = m.group(1)
        if who in {"tab", "this tab", "current tab", "this", "it", "this window", "current window", "window", "active window", "the window"}:
            return None
        if who in APPS or _match_windows(who):
            return close_app(who)
        return False, f"I don't see {who} open."

    if t in {"pause", "pause it", "pause the song", "pause music", "pause the music", "pause the video", "pause video",
             "stop the music", "stop music", "stop the song", "pause playback", "pause the track", "pause audio"}:
        return media("pause")
    if re.fullmatch(r"(?:resume|unpause|continue|play|continue playing|resume playing)(?: the| this)?(?: song| music| video| track| playback| audio)?", t):
        return media("play")
    if re.fullmatch(r"(?:next|skip|play next|next song|next track|skip this|skip the song|skip song|next video)(?: song| track)?", t):
        return media("next")
    if re.fullmatch(r"(?:previous|play previous|previous song|previous track|go back a song|last song|play the last song)", t):
        return media("previous")

    m = re.fullmatch(rf"(?:open|go to|click|select|show)(?: on)?(?: the)? ({_ORD_RE})(?: one)? chat(?: on whatsapp| in whatsapp)?", t)
    if m:
        n = _ord(m.group(1))
        if n:
            return whatsapp_open_chat(nth=n)
    m = re.fullmatch(r"(?:open|go to|show) (?:the )?chat (?:with|of|for) (.+?)(?: on whatsapp| in whatsapp)?", t) or \
        re.fullmatch(r"(?:open|go to) (.+?)(?:'s| s)? chat(?: on whatsapp| in whatsapp)?", t)
    if m and "whatsapp" in (t + " whatsapp" if _match_windows("whatsapp") else t):
        return whatsapp_open_chat(m.group(1).strip())
    m = (re.fullmatch(r"(?:send|write|type)(?: a)?(?: whatsapp)?(?: message| msg| text)? to (.+?)(?: on whatsapp| in whatsapp| via whatsapp)?(?: saying| that says| that| says|:)\s+(.+)", t)
         or re.fullmatch(r"(?:whatsapp|message|text|tell) (.+?)(?: on whatsapp)?(?: saying| that| to say|:)\s+(.+)", t))
    if m:
        return whatsapp_message(m.group(1).strip(), m.group(2).strip())

    m = re.fullmatch(r"(?:open|create|make|start)(?: a| an)?(?: new| blank) (notebook|colab notebook|document|doc|google doc|sheet|spreadsheet|slides|presentation|form)(?: in (?:google )?(?:colab|docs|sheets|slides|forms))?", t)
    if m:
        try:
            import tools
            return tools._navigate_existing_browser(NEW_DOCS[m.group(1)], f"a new {m.group(1)}")
        except Exception as e:
            return False, f"Couldn't open a new {m.group(1)}: {e}"

    m = re.fullmatch(r"(?:switch to|go to|focus|bring up|bring)(?: the)? (.+?)(?: window| app)?", t)
    if m and (m.group(1) in APPS or _match_windows(m.group(1))):
        return focus_app(m.group(1))


    if re.fullmatch(r"(?:what(?:'s| is| are)|which|list|show)(?: me)?(?: the)?(?: apps| windows| programs)?(?: that are| are)? (?:open|running)(?: right now)?|what(?:'s| is) open", t):
        return list_windows()
    if re.fullmatch(r"(?:what|which) (?:window|app) is (?:this|open|active|in front)", t):
        return True, "You're in " + (next((w[1] for w in _windows() if w[0] == __import__("ctypes").windll.user32.GetForegroundWindow()), "an unknown window")[:80]) + "."
    m = re.search(r"\b(battery|cpu|ram|memory)\b", t)
    if m and re.match(r"(?:what(?:'s| is)|how much|check|show|tell me)", t):
        return system_status(m.group(1))
    if re.fullmatch(r"(?:read|what(?:'s| is) on)(?: my| the)? clipboard|what did i copy", t):
        return read_clipboard()
    m = re.fullmatch(r"(?:take|make|add|write|save)(?: a| this)? note(?: that| about| to myself)?[:,]?\s*(.+)", t) or \
        re.fullmatch(r"(?:note|jot) (?:down )?(?:that )?(.+)", t)
    if m:
        return take_note(m.group(1))
    if re.fullmatch(r"(?:read|show)(?: me)?(?: my)?(?: latest| last)? notes", t):
        return read_notes()
    m = re.fullmatch(r"(?:set|start)(?: a)? timer(?: for)? (\d+) (seconds?|minutes?|hours?)", t)
    if m:
        return set_timer(int(m.group(1)), m.group(2))
    if re.fullmatch(r"(?:open|start|launch)(?: my)? workspace", t):
        return open_workspace()
    if re.fullmatch(r"(?:what(?:'s| is) on my screen|what am i looking at|describe(?: my| the)? screen|look at(?: my| the)? screen|read(?: my| the)? screen)", t):
        return describe_screen()

    m = re.fullmatch(r"(?:open|launch|start) (?:the )?(.+?)(?: app| application)?", t)
    if m and m.group(1) in APPS:
        return open_app(m.group(1))
    if m and not re.search(r"\.|\bsettings?\b|\btab\b|\bnew\b", m.group(1)):
        try:
            import tools
            reserved = set(tools._SITES) | set(tools._FOLDERS)
        except Exception:
            reserved = set()
        if m.group(1) not in reserved and resolve_start_app(m.group(1), cutoff=0.85):
            return launch_start_app(m.group(1))

    m = re.fullmatch(r"(double click|right click|click|tap|press|select|choose)(?: on)?(?: the)? (.+?)(?: button| icon| option| link| tab)?", t)
    if m and m.group(2) not in {"enter", "tab", "space", "escape", "esc", "backspace", "delete"} and not re.fullmatch(r"(?:f\d+|ctrl.*|alt.*)", m.group(2)):
        return smart_click(m.group(2), double=m.group(1) == "double click", right=m.group(1) == "right click")
    return None


def handle(text: str) -> Optional[Result]:
    """Main entry. Runs 1..n steps, verifies each, stops at the first failure and says which."""
    try:
        steps = split_steps(text)
        if not steps:
            return None
        if len(steps) == 1:
            return _single(steps[0])
        done, last = [], ""
        for i, s in enumerate(steps, 1):
            if aborted():
                return False, "Stopped, Chief." + (f" I had already done: {'; '.join(done)}." if done else "")
            r = _single(s)
            if r is None:  # not mine -> hand to the normal router, then the agent
                try:
                    import tools
                    r = tools.perform_action(s)
                except Exception:
                    r = None
                r = rescue(s, r) if r is not None else (False, f"I didn't understand the step '{s}'.")
            ok, msg = r
            if not ok:
                return False, (f"Step {i} failed: {msg} " + (f"I had already done: {'; '.join(done)}." if done else "")).strip()
            done.append(msg.rstrip("."))
            last = msg
            time.sleep(1.8 if re.match(r"(open|new|go|switch|create|start)", s) else 0.6)
        return True, f"Done. {last}"
    except Exception as e:
        log.exception("pc_control.handle crashed")
        return False, f"Something went wrong while controlling the PC: {e}"


RISKY = re.compile(r"\b(delete|remove|erase|format|uninstall|pay|purchase|buy|order|checkout|transfer|submit|post|publish|"
                   r"shut ?down|restart|sign ?out|log ?out|reset|wipe|empty)\b", re.I)
_UNSUPPORTED = "isn't available in VAANI's safe action list"


def agent(goal: str) -> Result:
    """Last resort: look at the screen, act, look again (up to 10 steps)."""
    global _pending
    if RISKY.search(goal):
        _pending = {"kind": "agent", "goal": goal}
        return True, f"That could change or send something. I'll do '{goal[:80]}' on the screen only if you say confirm."
    try:
        import screen_vision
        if not os.getenv("GEMINI_API_KEY", "").strip():
            return False, "The step-by-step screen agent needs the Gemini key in .env. Single clicks work locally."
        return screen_vision.screen_task(goal, max_steps=10)
    except Exception as e:
        return False, f"Screen agent failed: {e}"


def rescue(text: str, result: Optional[Result]) -> Optional[Result]:
    """Called with the old router's result: if it said 'unsupported' (or crashed), try the screen agent."""
    if result is None:
        return None
    ok, msg = result
    if not ok and _UNSUPPORTED in msg:
        return agent(text)
    return result