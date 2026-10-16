"""
VAANI MASTER ACTION ROUTER
One general-purpose action layer for everyday Windows laptop commands.

The local Ollama model converts natural language into a small JSON action plan.
Only actions in ACTIONS are executed. The model cannot execute shell commands,
Python, arbitrary code, or arbitrary keyboard shortcuts.
"""
from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import time
from collections import deque

import env_loader  # noqa: F401  (loads GEMINI_API_KEY from .env)
import friendly
import levels
import verify
import screen_state
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Optional, Tuple

Result = Tuple[bool, str]
log = logging.getLogger("vaani.tools")
MODEL = "qwen2.5:3b-instruct"
_pending_action: Optional[dict[str, Any]] = None

ACTIONS = {
    "open_app": "Open an installed Windows application by its display name using Start-menu search.",
    "close_window": "Close the currently active window using Alt+F4. The app may show its own save prompt.",
    "switch_window": "Switch to the next open window using Alt+Tab.",
    "minimize_window": "Minimize the active window.",
    "maximize_window": "Maximize the active window.",
    "type_text": "Paste exact user-provided text into the active window.",
    "press_key": "Press one allowed key such as enter, tab, escape, backspace, delete, arrows, home, end, pageup, pagedown, space.",
    "hotkey": "Run one allowed common shortcut such as copy, paste, cut, select_all, save, undo, redo, find, new_tab, close_tab, refresh, print, screenshot, lock.",
    "scroll": "Scroll the active window up or down.",
    "volume": "Raise/lower/mute system volume.",
    "brightness": "Raise/lower screen brightness when the display supports it.",
    "open_settings": "Open a supported Windows Settings page (bluetooth, wifi, network, sound, display, personalization, apps, accessibility, accounts, date_time, updates, storage, notifications, clipboard, power).",
    "open_website": "Open a named website or a safe HTTP/HTTPS URL in the default browser, reusing an existing browser window when possible.",
    "current_tab_media": "On the current browser search-results page, switch to Images or Videos while preserving the current search query and reusing the same tab.",
    "navigate_current_tab": "Navigate the existing browser tab to a requested URL or search, reusing the current Chrome window rather than opening a new window.",
    "web_search": "Search the web for the requested query.",
    "youtube_search": "Open YouTube search results for a song or video query. Playback may require selecting a result.",
    "open_folder": "Open a common user folder in File Explorer: home, desktop, downloads, documents, pictures, videos.",
    "calculate": "Evaluate basic arithmetic only.",
    "lock_pc": "Lock the Windows session.",
    "open_profile_picker": "Open Chrome's profile picker. Never choose a profile automatically.",
    "screenshot": "Capture a screenshot and save it in the user's Pictures/VAANI_Screenshots folder.",
    "delete_active_text": "Select and delete all text in the active editor. This is destructive and requires confirmation.",
    "chat": "This is a normal question or conversation, not a computer action.",
    "unsupported_action": "A requested computer action is not currently supported by the safe action list."
}

CLASSIFIER_PROMPT = """
You are VAANI's local command interpreter. Convert the user's latest utterance
into exactly one JSON object with these fields:
{"action":"one action name","target":"short app/site/settings/folder/direction name or empty string","text":"exact text to type or empty string","key":"key name or shortcut name or empty string","query":"search/video query or arithmetic expression or empty string","direction":"up/down/mute or empty string"}.

Choose only an action name from this list:
""" + "\n".join(f"- {name}: {description}" for name, description in ACTIONS.items()) + """

Rules:
- Interpret natural language flexibly, including synonyms, polite phrasing, and follow-up-like commands.
- Do not output code, shell commands, Python, or invented action names.
- For "type/write/say this into the active window: ...", action=type_text and text must preserve the requested text.
- For "next line", "move cursor to next line", "press return", use press_key with key=enter.
- For "close Notepad/this app/current window", use close_window. It closes the ACTIVE window; do not claim to target a background app.
- For "open [app]", use open_app and target=app name. For websites use open_website.
- IMPORTANT: "open a new tab", "open new tab", "new tab", "new browser tab" are hotkey action with key=new_tab, NOT open_app.
- "close this tab" / "close current tab" are hotkey with key=close_tab, NOT close_window.
- "open Chrome" means activate/open Chrome, not type Chrome into Start if it is already running.
- If the user says "images" or "videos" and refers to the current tab/current search/page, use current_tab_media with target=images or videos, preserving the current search query.
- If the user explicitly says "in this tab", "current tab", "same tab", "on this page", or "here", use navigate_current_tab for navigation/search rather than opening a new browser tab.
- For songs/videos to find on YouTube, use youtube_search.
- For "search the web for X", use web_search and query=X.
- For volume and brightness, use direction=up/down/mute/max/min. If the user gives an exact percent (\"volume to 50 percent\"), set \"level\" to that number (0-100) and direction=set.
- For Windows Settings, use open_settings and target as one of the supported page names.
- For delete/erase/clear all text in the current document/editor, use delete_active_text. Never map deleting files/folders to this action.
- For opening a particular Chrome account/profile, use open_profile_picker; do not claim you can pick a numbered account.
- For ordinary questions, greetings, explanations, or requests for advice, use chat.
- If a computer action is not in the available action list, use unsupported_action.
- Output JSON only. Do not include a success message; the program will report what actually happened.
- If the user gives an exact percent for volume/brightness, add a numeric field \"level\" (0-100) to the JSON object.
"""


def _pg():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.12
        return pyautogui
    except Exception:
        return None


def _clipboard_paste(value: str) -> Result:
    if not value:
        return False, "I didn't receive any text to type."
    pg = _pg()
    if pg is None:
        return False, "Keyboard automation isn't available. Install pyautogui in VAANI's existing environment."
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update()
        root.destroy()
        pg.hotkey("ctrl", "v")
        return True, "Typed the requested text into the active window."
    except Exception as exc:
        return False, f"Typing failed: {exc}"


def _open_app(name: str) -> Result:
    name = name.strip()
    if not name:
        return False, "Tell me which application to open."

    # Browser apps should activate an existing browser, not type their name into Start.
    normalized = re.sub(r"\s+", " ", name.lower()).strip()
    if normalized in {"chrome", "google chrome", "browser", "web browser"}:
        if _find_chrome_window():
            return True, "Brought your existing Google Chrome window to the foreground."
        # If Chrome is not already open, use the registered application association.
        try:
            import subprocess
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
            for executable in candidates:
                if os.path.isfile(executable):
                    subprocess.Popen([executable], close_fds=True)
                    return True, "Opened Google Chrome."
            webbrowser.open("https://www.google.com", new=1)
            return True, "Opened your default browser because a Chrome installation wasn't found in its usual locations."
        except Exception as exc:
            return False, f"Couldn't open Google Chrome: {exc}"

    # Use Start-menu search only for other apps; never for browser-control commands.
    pg = _pg()
    if pg is None:
        return False, "Application search needs pyautogui installed in VAANI's existing environment."
    try:
        pg.press("win")
        import time
        time.sleep(0.35)
        pg.write(name[:100], interval=0.025)
        time.sleep(0.8)
        pg.press("enter")
        return True, f"Sent '{name}' to Windows Start-menu search. Check that the intended application opened."
    except Exception as exc:
        return False, f"Couldn't launch '{name}' through Start-menu search: {exc}"


_NOT_CHROME = ("visual studio code", "powershell", "terminal", "command prompt", "vaani", "notepad")


def _is_chrome_title(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t or any(x in t for x in _NOT_CHROME):
        return False
    return bool(re.search(r"\bgoogle chrome\b", t)) or t == "chrome"


def _foreground_title() -> str:
    try:
        import pygetwindow
        w = pygetwindow.getActiveWindow()
        return (w.title or "").strip() if w else ""
    except Exception:
        return ""


def _chrome_in_front() -> bool:
    """True when Chrome is what the user is looking at (or when we can't tell)."""
    fg = _foreground_title()
    return (not fg) or _is_chrome_title(fg)


def _force_foreground(win) -> bool:
    """Windows blocks background apps from taking focus; the Alt-key + thread-attach trick gets around that."""
    try:
        import ctypes
        hwnd = getattr(win, "_hWnd", None)
        if not hwnd:
            return False
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)                                   # SW_RESTORE
        if u32.GetForegroundWindow() == hwnd:
            return True
        u32.keybd_event(0x12, 0, 0, 0)                                # tap Alt: lets SetForegroundWindow succeed
        u32.keybd_event(0x12, 0, 2, 0)
        cur, fg_thread = k32.GetCurrentThreadId(), u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
        u32.AttachThreadInput(cur, fg_thread, True)
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            u32.AttachThreadInput(cur, fg_thread, False)
        return u32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _find_chrome_window():
    """True only if a Chrome window is (or has now been brought) in front, so keystrokes can't hit the wrong app."""
    try:
        import pygetwindow
        active = pygetwindow.getActiveWindow()
        if active is not None and _is_chrome_title(getattr(active, "title", "")):
            return True
        wins = [w for w in pygetwindow.getAllWindows() if _is_chrome_title(w.title)]
        if not wins:
            return False
        win = wins[0]
        for _ in range(2):
            try:
                if getattr(win, "isMinimized", False):
                    win.restore()
                win.activate()
            except Exception:
                pass                                                  # pygetwindow raises even when it half-worked
            time.sleep(0.2)
            now = pygetwindow.getActiveWindow()
            if (now is not None and _is_chrome_title(getattr(now, "title", ""))) or _force_foreground(win):
                time.sleep(0.15)
                return True
        return False
    except Exception:
        return False


def _clipboard_set(value: str) -> bool:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


def _clipboard_get() -> str:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        value = root.clipboard_get()
        root.destroy()
        return value.strip()
    except Exception:
        return ""


def _current_browser_url() -> str:
    pg = _pg()
    if pg is None or not _find_chrome_window():
        return ""
    try:
        old_clipboard = _clipboard_get()
        pg.hotkey("ctrl", "l")
        pg.hotkey("ctrl", "c")
        import time
        time.sleep(0.18)
        url = _clipboard_get()
        pg.press("esc")
        if old_clipboard:
            _clipboard_set(old_clipboard)
        return url
    except Exception:
        try:
            pg.press("esc")
        except Exception:
            pass
        return ""


def _navigate_existing_browser(url: str, label: str) -> Result:
    """Extension first (reliable, no focus needed). Fallback: type the URL into Chrome's address bar (no clipboard)."""
    if _bridge_ready():
        ok, payload = _bridge_call("navigate", url=url)
        if ok:
            return True, f"Opened {label}."
        log.warning("bridge navigate failed (%s); falling back to keyboard navigation", payload)
    pg = _pg()
    if pg is not None and _find_chrome_window():
        try:
            pg.hotkey("ctrl", "l")
            time.sleep(0.12)
            pg.write(url, interval=0.002)
            pg.press("enter")
            return True, f"Opened {label} in your current Chrome tab."
        except Exception as exc:
            return False, f"Couldn't navigate the existing Chrome tab: {exc}"
    try:
        webbrowser.open(url, new=0)
        return True, f"Opened {label} in your default browser. No existing Chrome window was found to reuse."
    except Exception as exc:
        return False, f"Couldn't open {label}: {exc}"


def _current_tab_media(target: str) -> Result:
    mode = target.lower().strip()
    if mode not in {"images", "image", "videos", "video"}:
        return False, "Say images or videos."
    mode = "images" if mode in {"images", "image"} else "videos"
    current_url = _current_browser_url()
    if not current_url:
        return False, "I couldn't read an open Chrome tab. Bring Chrome to the foreground and try again."
    try:
        parsed = urllib.parse.urlparse(current_url)
        host = (parsed.hostname or "").lower()
        params = urllib.parse.parse_qs(parsed.query)
        if "google." in host and parsed.path.rstrip("/") == "/search":
            query = (params.get("q") or [""])[0]
            if not query:
                return False, "The current Google page doesn't contain a search query to preserve."
            url = "https://www.google.com/search?" + urllib.parse.urlencode({
                "q": query, "tbm": "isch" if mode == "images" else "vid"
            })
            return _navigate_existing_browser(url, f"Google {mode} results for '{query}'")
        if "bing.com" in host and parsed.path.rstrip("/") == "/search":
            query = (params.get("q") or [""])[0]
            if not query:
                return False, "The current Bing page doesn't contain a search query to preserve."
            base = "https://www.bing.com/images/search" if mode == "images" else "https://www.bing.com/videos/search"
            return _navigate_existing_browser(base + "?" + urllib.parse.urlencode({"q": query}), f"Bing {mode} results for '{query}'")
        return False, (
            f"The current tab is {host or 'an unknown page'}, not a recognized Google/Bing search-results page. "
            f"I can reuse the tab, but I can't safely identify that site's '{mode}' control yet."
        )
    except Exception as exc:
        return False, f"Couldn't interpret the current browser page: {exc}"


def _open_url(url: str, label: str) -> Result:
    url = url.strip()
    if not url:
        return False, "Tell me which website to open."
    if not re.match(r"^https?://", url, re.I):
        if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?", url):
            url = "https://" + url
        else:
            return False, "I couldn't verify that as a website address."
    return _navigate_existing_browser(url, label)


def _settings(target: str) -> Result:
    pages = {
        "bluetooth": "ms-settings:bluetooth",
        "wifi": "ms-settings:network-wifi",
        "wi-fi": "ms-settings:network-wifi",
        "network": "ms-settings:network-status",
        "sound": "ms-settings:sound",
        "audio": "ms-settings:sound",
        "display": "ms-settings:display",
        "screen": "ms-settings:display",
        "personalization": "ms-settings:personalization",
        "apps": "ms-settings:appsfeatures",
        "accessibility": "ms-settings:easeofaccess",
        "accounts": "ms-settings:yourinfo",
        "date_time": "ms-settings:dateandtime",
        "date and time": "ms-settings:dateandtime",
        "updates": "ms-settings:windowsupdate",
        "windows update": "ms-settings:windowsupdate",
        "storage": "ms-settings:storagesense",
        "notifications": "ms-settings:notifications",
        "clipboard": "ms-settings:clipboard",
        "power": "ms-settings:powersleep",
        "settings": "ms-settings:",
    }
    key = target.lower().strip()
    uri = pages.get(key)
    if not uri:
        return False, f"I don't have a Windows Settings page configured for '{target}'."
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return True, f"Opened Windows {target} settings. Complete any toggle or selection there."
    except Exception as exc:
        return False, f"Couldn't open {target} settings: {exc}"


def _calculate(expression: str) -> Result:
    expr = expression.lower().strip()
    for phrase, symbol in [
        ("multiplied by", "*"), ("multiply by", "*"), ("times", "*"),
        ("divided by", "/"), ("divide by", "/"), ("plus", "+"),
        ("minus", "-"), ("to the power of", "**"), ("power of", "**"),
        ("modulo", "%"), ("remainder", "%"), ("×", "*"), ("÷", "/"),
    ]:
        expr = expr.replace(phrase, symbol)
    expr = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", expr)
    expr = re.sub(r"[^0-9.+*/()\-%%\s]", "", expr)
    try:
        tree = ast.parse(expr, mode="eval")
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub,
                   ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Load)
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                return False, "I can calculate arithmetic expressions only."
            if isinstance(node, ast.Constant) and type(node.value) not in (int, float):
                return False, "That calculation contains an unsupported value."
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                if isinstance(node.right, ast.Constant) and abs(node.right.value) > 10:
                    return False, "That exponent is too large."
        result = eval(compile(tree, "<vaani-arithmetic>", "eval"), {"__builtins__": {}}, {})
        if not math.isfinite(float(result)):
            return False, "That calculation doesn't produce a finite result."
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return True, f"The answer is {result}."
    except Exception:
        return False, "I couldn't understand that calculation. Try saying, 'calculate 245 multiplied by 18'."


def _run_hotkey(key: str) -> Result:
    pg = _pg()
    if pg is None:
        return False, "Keyboard automation isn't available. Install pyautogui in VAANI's existing environment."
    shortcuts = {
        "copy": ("ctrl", "c"), "paste": ("ctrl", "v"), "cut": ("ctrl", "x"),
        "select_all": ("ctrl", "a"), "select all": ("ctrl", "a"),
        "save": ("ctrl", "s"), "undo": ("ctrl", "z"), "redo": ("ctrl", "y"),
        "find": ("ctrl", "f"), "print": ("ctrl", "p"),
        "new_tab": ("ctrl", "t"), "new tab": ("ctrl", "t"),
        "close_tab": ("ctrl", "w"), "close tab": ("ctrl", "w"),
        "refresh": ("ctrl", "r"), "reload": ("ctrl", "r"),
        "switch_window": ("alt", "tab"), "switch window": ("alt", "tab"),
        "minimize": ("win", "down"), "minimize_window": ("win", "down"),
        "maximize": ("win", "up"), "maximize_window": ("win", "up"),
        "lock": ("win", "l"), "lock computer": ("win", "l"),
        "screenshot": ("win", "shift", "s"), "snip": ("win", "shift", "s"),
        "show_desktop": ("win", "d"), "task_manager": ("ctrl", "shift", "esc"), "fullscreen": ("f11",),
    }
    shortcut = shortcuts.get(key.lower().strip())
    if not shortcut:
        return False, f"That shortcut isn't in VAANI's safe shortcut list: {key}."
    try:
        pg.hotkey(*shortcut)
        return True, f"Performed the {key.replace('_', ' ')} shortcut."
    except Exception as exc:
        return False, f"Shortcut failed: {exc}"


def _press_key(key: str) -> Result:
    pg = _pg()
    if pg is None:
        return False, "Keyboard automation isn't available. Install pyautogui in VAANI's existing environment."
    aliases = {
        "return": "enter", "newline": "enter", "new line": "enter",
        "escape": "esc", "spacebar": "space", "left arrow": "left",
        "right arrow": "right", "up arrow": "up", "down arrow": "down",
        "page up": "pageup", "page down": "pagedown",
    }
    normalized = aliases.get(key.lower().strip(), key.lower().strip())
    allowed = {"enter", "tab", "esc", "backspace", "delete", "left", "right", "up", "down",
               "home", "end", "pageup", "pagedown", "space", "insert"}
    if normalized not in allowed:
        return False, f"That key isn't in VAANI's safe key list: {key}."
    try:
        pg.press(normalized)
        return True, f"Pressed {normalized}."
    except Exception as exc:
        return False, f"Key press failed: {exc}"


def _scroll(direction: str) -> Result:
    """Raw mouse-wheel scroll. NOTE: on Windows pyautogui's scroll() takes wheel units where one notch is 120,
    so the old pg.scroll(5) moved the page by almost nothing. 600 = five notches, a comfortable page-ish step."""
    pg = _pg()
    if pg is None:
        return False, "Scrolling needs pyautogui installed in VAANI's existing environment."
    try:
        pg.scroll(600 if direction.lower() == "up" else -600)
        return True, friendly.pick("scroll_ok", d=direction.lower())
    except Exception as exc:
        return False, f"Scrolling failed: {exc}"


def _close_active_window() -> Result:
    """Close the active window and PROVE it closed. Tries Alt+F4, then WM_CLOSE, then the title-bar Close button."""
    pg = _pg()
    if pg is None:
        return False, "Closing windows needs pyautogui installed in VAANI's existing environment."
    start = screen_state.foreground()
    title = start.get("title") or "that window"
    short = friendly.short_name(title, "") if title else "the window"

    def alt_f4():
        pg.hotkey("alt", "f4")
        return True, ""

    def wm_close():
        import ctypes
        hwnd = start.get("hwnd")
        if not hwnd:
            return False, "no window handle"
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return True, ""

    def close_button():
        import pc_control
        return pc_control.uia_click("Close", kinds=("ButtonControl",))

    state = {"dialog": False}

    def check(before, after):
        now = after["windows"]
        if title and title not in now:
            return True, ["window disappeared"]
        if before["fg"].get("hwnd") != after["fg"].get("hwnd"):      # a new window popped up in front: a save prompt
            state["dialog"] = True
            return True, ["a prompt appeared"]
        return False, []

    ok, _msg, trace = verify.attempt(f"close {short}", [("Alt+F4", alt_f4), ("window close message", wm_close),
                                                         ("title-bar button", close_button)], check, settle=0.5, timeout=1.5)
    if not ok:
        return False, f"{short} didn't close. It may be stuck or asking something I can't see."
    if state["dialog"]:
        return True, f"{short} is asking something before it closes. Check the prompt, Chief."
    return True, f"Closed {short}."


def _smart_scroll(direction: str) -> Result:
    """Scroll and PROVE the page moved. Methods (see scrolling.py): browser extension, wheel message, mouse wheel, keyboard.
    Success is judged by measuring that the screen content really slid, not by "something changed" (a video would fool that)."""
    import scrolling
    d = (direction or "down").lower()
    if d not in {"up", "down", "top", "bottom"}:
        d = "down"
    chrome_front = _is_chrome_title(_foreground_title())

    def extension(dd: str) -> bool:
        if not (chrome_front and _bridge_ready()):
            return False
        ok, payload = _bridge_call("scroll", direction=dd)
        return bool(ok and isinstance(payload, dict) and payload.get("moved") is True)

    ok, how = scrolling.scroll(d, extension=extension)
    spoken = ("to the " + d) if d in {"top", "bottom"} else d
    if ok:
        logging.getLogger("vaani.tools").info("scroll %s worked via %s", d, how)
        return True, friendly.pick("scroll_ok", d=spoken)
    logging.getLogger("vaani.tools").warning("scroll %s failed: %s", d, how)
    where = {"down": "bottom", "up": "top"}.get(d, d)
    return False, f"The page didn't move. It may already be at the {where}, Chief."


def _volume(direction: str) -> Result:
    """Mute toggle, or an exact 10% step up/down (use levels.adjust for percentages)."""
    d = direction.lower()
    if d in {"mute", "unmute"}:
        pg = _pg()
        if pg is None:
            return False, "Volume hotkeys need pyautogui installed in VAANI's existing environment."
        try:
            pg.press("volumemute")
            return True, "Volume mute toggled."
        except Exception as exc:
            return False, f"Volume control failed: {exc}"
    if d not in {"up", "down"}:
        return False, "Tell me to turn volume up, down, mute it, or set a percent."
    return levels.adjust("volume", f"volume {d}")


def _brightness(direction: str) -> Result:
    d = direction.lower()
    if d not in {"up", "down"}:
        return False, "Tell me to make the screen brighter, dimmer, or set a percent."
    return levels.adjust("brightness", f"brightness {d}")


def _open_folder(target: str) -> Result:
    folders = {
        "home": Path.home(),
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "pictures": Path.home() / "Pictures",
        "videos": Path.home() / "Videos",
    }
    folder = folders.get(target.lower().strip())
    if folder is None:
        return False, f"I can open common folders, but '{target}' isn't on the folder allow-list."
    try:
        os.startfile(str(folder))  # type: ignore[attr-defined]
        return True, f"Opened {target} in File Explorer."
    except Exception as exc:
        return False, f"Couldn't open {target}: {exc}"


def _screenshot() -> Result:
    pg = _pg()
    if pg is None:
        return False, "Screenshots need pyautogui installed in VAANI's existing environment."
    try:
        directory = Path.home() / "Pictures" / "VAANI_Screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        import datetime
        destination = directory / ("screenshot_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png")
        pg.screenshot().save(str(destination))
        return True, f"Screenshot saved to {destination}."
    except Exception as exc:
        return False, f"Couldn't save screenshot: {exc}"


def _confirm_or_cancel(text: str) -> Optional[Result]:
    global _pending_action
    normalized = text.lower().strip().rstrip(".!?")
    cancel = {"cancel", "cancel action", "no", "don't do it", "do not do it", "cancel delete", "don't send", "do not send", "cancel send"}
    confirm = {"confirm", "confirm action", "confirm delete", "yes confirm", "do it", "confirm send", "send it", "yes send it", "yes"}
    if normalized in cancel:
        if _pending_action is not None:
            _pending_action = None
            return True, "Cancelled. Nothing was changed."
        return None
    if normalized in confirm:
        if _pending_action is None:
            # "yes" / "send it" alone are ordinary words when nothing is pending.
            return None if normalized in {"yes", "send it", "yes send it"} else (False, "There is no pending action to confirm.")
        pending = _pending_action
        _pending_action = None
        if pending.get("action") == "delete_active_text":
            pg = _pg()
            if pg is None:
                return False, "Keyboard automation isn't available."
            try:
                pg.hotkey("ctrl", "a")
                pg.press("backspace")
                return True, "Selected and deleted text in the active window."
            except Exception as exc:
                return False, f"Deletion failed: {exc}"
        if pending.get("action") == "screen_task":
            try:
                import desktop_agent
                return desktop_agent.run(pending["goal"], max_steps=10, announce=_announce, allow_risky=True)
            except Exception as exc:
                return False, f"Screen task failed: {exc}"
        if pending.get("action") == "mcp":
            import mcp_client
            return mcp_client.call(pending["server"], pending["tool"], pending.get("args") or {})
        if pending.get("action") == "send_whatsapp":
            return _do_whatsapp(pending["contact"], pending["message"])
        return False, "That pending action isn't supported."
    return None


def _classify_with_ollama(user_text: str) -> Optional[dict[str, Any]]:
    try:
        import ollama
        response = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": user_text},
            ],
            format="json",
            options={"temperature": 0},
        )
        content = response.message.content or "{}"
        plan = json.loads(content)
        if not isinstance(plan, dict) or plan.get("action") not in ACTIONS:
            return None
        return plan
    except Exception as exc:
        print(f"[VAANI action router] Could not interpret command: {exc}")
        return None


def _execute(plan: dict[str, Any]) -> Optional[Result]:
    action = plan.get("action", "")
    target = str(plan.get("target") or "").strip()
    text = str(plan.get("text") or "")
    key = str(plan.get("key") or "").strip()
    query = str(plan.get("query") or "").strip()
    direction = str(plan.get("direction") or "").strip().lower()

    if action == "open_app":
        return _open_app(target)
    if action == "close_window":
        return _close_active_window()
    if action == "switch_window":
        return _run_hotkey("switch_window")
    if action == "minimize_window":
        return _run_hotkey("minimize_window")
    if action == "maximize_window":
        return _run_hotkey("maximize_window")
    if action == "type_text":
        return _clipboard_paste(text)
    if action == "press_key":
        return _press_key(key)
    if action == "hotkey":
        return _run_hotkey(key)
    if action == "scroll":
        return _smart_scroll(direction if direction in {"up", "down", "top", "bottom"} else "down")
    if action in {"volume", "brightness"}:
        lvl = plan.get("level")
        try:
            lvl = int(re.sub(r"[^\d]", "", str(lvl))) if lvl not in (None, "") else None
        except ValueError:
            lvl = None
        if action == "volume" and direction in {"mute", "unmute"}:
            return _volume("mute")
        if lvl is not None:
            return levels.adjust(action, f"{action} to {lvl} percent")
        return levels.adjust(action, f"{action} {direction or 'up'}")
    if action == "open_settings":
        return _settings(target)
    if action == "current_tab_media":
        return _current_tab_media(target or query)
    if action == "navigate_current_tab":
        destination = target or query
        if not destination:
            return False, "Tell me what page or search you want in the current tab."
        known_sites = {
            "google": "https://www.google.com",
            "youtube": "https://www.youtube.com",
            "gmail": "https://mail.google.com",
            "github": "https://github.com",
            "images": "https://images.google.com",
            "google images": "https://images.google.com",
        }
        if destination.lower() in known_sites:
            return _navigate_existing_browser(known_sites[destination.lower()], destination)
        if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?", destination):
            return _open_url(destination, destination)
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(destination)
        return _navigate_existing_browser(url, f"search results for {destination}")
    if action == "open_website":
        website = target
        known = {
            "google": "https://www.google.com",
            "youtube": "https://www.youtube.com",
            "gmail": "https://mail.google.com",
            "google drive": "https://drive.google.com",
            "github": "https://github.com",
            "linkedin": "https://www.linkedin.com",
            "whatsapp": "https://web.whatsapp.com",
            "chatgpt": "https://chatgpt.com",
            "outlook": "https://outlook.live.com",
            "calendar": "https://calendar.google.com",
        }
        if website.lower() in known:
            return _navigate_existing_browser(known[website.lower()], website)
        return _open_url(website, website)
    if action == "web_search":
        if not query:
            return False, "Tell me what you want to search for."
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        return _open_url(url, f"a web search for {query}")
    if action == "youtube_search":
        if not query:
            query = target
        if not query:
            return False, "Tell me the song or video name."
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
        return _open_url(url, f"YouTube search results for {query}. Choose a result to start playback.")
    if action == "open_folder":
        return _open_folder(target)
    if action == "calculate":
        return _calculate(query or text or target)
    if action == "lock_pc":
        return _run_hotkey("lock")
    if action == "open_profile_picker":
        return _open_url("https://www.google.com", "your browser")
    if action == "screenshot":
        return _screenshot()
    if action == "delete_active_text":
        global _pending_action
        _pending_action = {"action": "delete_active_text"}
        return True, "This will select and delete all text in the active window. Make sure the correct document is active and saved if needed. Say 'confirm delete' to proceed or 'cancel' to leave it unchanged."
    if action == "chat":
        return None
    if action == "unsupported_action":
        return False, "Chief, that action isn't available in VAANI's safe action list yet. I haven't performed it."
    return None




# =====================================================================
#  BROWSER BRIDGE HELPERS
# =====================================================================
def _bridge_ready() -> bool:
    try:
        from browser_bridge import browser_ready
        return bool(browser_ready())
    except Exception:
        return False


def _bridge_call(action: str, timeout: float = 10, **kwargs):
    """Returns (ok, payload_or_error_text). Never raises."""
    try:
        from browser_bridge import command
        r = command(action, timeout=timeout, **kwargs)
    except Exception as exc:
        return False, f"Browser bridge is unavailable: {exc}"
    if not r.get("ok"):
        log.warning("bridge %s failed [%s]: %s", action, r.get("stage"), r.get("error"))
        return False, r.get("error") or "Chrome did not confirm the browser action."
    return True, r.get("result")


_NOT_CONNECTED = ("Chrome control isn't connected, Chief. Make sure Chrome is open, the VAANI Browser Bridge "
                  "extension is enabled, and VAANI's bridge is running.")


def _bridge_result(action: str, **kwargs) -> Result:
    """Run a bridge action and turn the payload into a short, speakable sentence."""
    if not _bridge_ready():
        return False, _NOT_CONNECTED
    ok, payload = _bridge_call(action, **kwargs)
    if not ok:
        return False, str(payload)
    return True, _speakable(action, payload)


def _speakable(action: str, payload) -> str:
    """What gets said out loud: short, human names ('GitHub'), never raw page titles or URLs."""
    p = payload if isinstance(payload, dict) else {}
    if action in {"click_result", "click_link", "click_text", "click_button"}:
        c = p.get("clicked")
        text = c.get("text") if isinstance(c, dict) else c
        label = friendly.short_label(text) if text else ""
        return friendly.pick("click", name=label) if label else friendly.pick("click_plain")
    if action == "switch_tab":
        return friendly.pick("switch_tab", name=friendly.short_name(p.get("title") or "", p.get("url") or ""))
    if action in {"search", "type"}:
        return friendly.pick("typed") + (" And I submitted it." if p.get("submitted") else "")
    if action == "new_tab":
        return friendly.pick("new_tab")
    if action == "close_tab":
        return friendly.pick("close_tab")
    if action == "scroll":
        return friendly.pick("scroll_ok", d=p.get("scrolled", "down"))
    return friendly.pick("generic")


def _page_snapshot():
    """(ok, state_or_error)"""
    if not _bridge_ready():
        return False, _NOT_CONNECTED
    ok, payload = _bridge_call("snapshot", timeout=8)
    if not ok:
        return False, str(payload)
    state = payload.get("page") if isinstance(payload, dict) and isinstance(payload.get("page"), dict) else payload
    return True, state if isinstance(state, dict) else {}


def _describe_page(state: dict) -> str:
    host = urllib.parse.urlparse(state.get("url", "")).hostname or ""
    out = f"You're on {friendly.short_name(state.get('title') or '', state.get('url') or '') if (state.get('title') or host) else 'an untitled page'}."
    res = state.get("results") or []
    if res:
        out += " The top results are: " + "; ".join(f"{r['index']}. {friendly.short_label(r['text'], 50)}" for r in res[:4]) + "."
    return out


def _summarize_page(state: dict) -> Result:
    text = (state.get("text") or "").strip()
    if not text:
        return False, "That page has no readable text yet."
    try:
        import ollama
        resp = ollama.chat(model=MODEL, options={"temperature": 0.2}, messages=[
            {"role": "system", "content": "Summarize this web page in at most three short sentences suitable for being read aloud. Use only the provided text. Do not invent details."},
            {"role": "user", "content": f"Title: {state.get('title','')}\n\n{text[:6000]}"},
        ])
        return True, (resp.message.content or "").strip() or _describe_page(state)
    except Exception as exc:
        log.exception("summarize failed")
        return False, f"I couldn't summarize the page: {exc}"


def _click_nth(n: int, kind: str) -> Result:
    if kind == "tab":
        if _bridge_ready():
            ok, payload = _bridge_call("goto_tab", index=n)
            return (True, _speakable("switch_tab", payload)) if ok else (False, str(payload))
        pg = _pg()
        if pg is None or not _find_chrome_window():
            return False, "I couldn't find an open Chrome window."
        pg.hotkey("ctrl", str(min(n, 8)))
        return True, f"Switched to tab {n}."
    if not _bridge_ready():
        return False, _NOT_CONNECTED
    # "the first link / result / video / website" on a results page means the Nth RESULT; on other pages, the Nth link
    ok, payload = _bridge_call("click_result", index=n)
    if ok:
        return True, _speakable("click_result", payload)
    if "can't find search results" in str(payload):
        return _bridge_result("click_link", index=n)
    return False, str(payload)


def _current_host() -> str:
    """Host of the page the user is looking at (via the extension); '' if unknown."""
    if not _bridge_ready():
        return ""
    ok, payload = _bridge_call("ping", timeout=4)
    if ok and isinstance(payload, dict):
        tab = payload.get("activeTab") or {}
        return (urllib.parse.urlparse(tab.get("url", "")).hostname or "").lower()
    return ""


def _screen_click(description: str, kind: str = "click") -> Result:
    try:
        import screen_vision
    except Exception as exc:
        return False, f"Screen vision isn't available: {exc}"
    return screen_vision.click_described(description, double=(kind == "double"), right=(kind == "right"))


def _screen_describe(question: str = "") -> Result:
    try:
        import screen_vision
    except Exception as exc:
        return False, f"Screen vision isn't available: {exc}"
    return screen_vision.describe(question)


def _youtube(query: str, play: bool) -> Result:
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    ok, msg = _navigate_existing_browser(url, f"YouTube results for {query}")
    if not ok or not play:
        return ok, msg if not ok else f"Showing YouTube results for {query}."
    if not _bridge_ready():
        return True, f"Showing YouTube results for {query}. Say 'open the first video' to play one."
    last = ""
    for _ in range(14):                      # results render a moment after the page loads
        time.sleep(0.5)
        ok2, payload = _bridge_call("click_result", index=1, timeout=6)
        if ok2:
            c = payload.get("clicked", {}) if isinstance(payload, dict) else {}
            return True, f"Playing {str(c.get('text') or query)[:90]}."
        last = str(payload)
    return False, f"I opened the YouTube results for {query} but couldn't start the first video. {last}"


# =====================================================================
#  WHATSAPP (asks for confirmation before sending anything)
# =====================================================================
def _do_whatsapp(contact: str, message: str) -> Result:
    pg = _pg()
    if pg is None:
        return False, "Keyboard automation isn't available."
    _navigate_existing_browser("https://web.whatsapp.com", "WhatsApp Web")
    time.sleep(2.5)
    try:
        pg.hotkey("ctrl", "alt", "/")
        time.sleep(0.4)
        pg.write(contact, interval=0.04)
        time.sleep(0.9)
        pg.press("enter")
        time.sleep(0.8)
        _clipboard_paste(message)
        pg.press("enter")
        return True, f"Sent your message to {contact}. Please verify it went to the right chat."
    except Exception as exc:
        return False, f"WhatsApp action failed: {exc}"


# =====================================================================
#  DETERMINISTIC LOCAL COMMANDS (fast, no AI, always the same result)
# =====================================================================
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
        "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8,
        "ninth": 9, "9th": 9, "tenth": 10, "10th": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_ORD_RE = r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th)?|one|two|three|four|five|number \d{1,2})"
_KIND_RE = r"(result|video|link|website|site|page|song|item|option|tab)"
_PLAIN_ORDINALS = {"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"}

_SITES = {"google": "https://www.google.com", "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com",
          "github": "https://github.com", "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
          "linkedin": "https://www.linkedin.com", "whatsapp": "https://web.whatsapp.com", "whatsapp web": "https://web.whatsapp.com",
          "chatgpt": "https://chatgpt.com", "outlook": "https://outlook.live.com", "calendar": "https://calendar.google.com",
          "discord": "https://discord.com/app", "spotify": "https://open.spotify.com", "classroom": "https://classroom.google.com",
          "maps": "https://maps.google.com", "google maps": "https://maps.google.com", "news": "https://news.google.com",
          "wikipedia": "https://www.wikipedia.org", "twitter": "https://x.com", "instagram": "https://www.instagram.com",
          "facebook": "https://www.facebook.com", "amazon": "https://www.amazon.com", "netflix": "https://www.netflix.com",
          "reddit": "https://www.reddit.com", "stack overflow": "https://stackoverflow.com"}
_FOLDERS = {"home", "desktop", "downloads", "documents", "pictures", "videos"}
_NOT_AN_APP = {"video", "videos", "result", "results", "link", "links", "website", "page", "tab", "chat", "profile", "image", "images", "search", "button"}


def _parse_ordinal(token: str) -> int:
    token = token.replace("number ", "").strip()
    if token in _ORD:
        return _ORD[token]
    m = re.match(r"\d+", token)
    return int(m.group()) if m else 1


def _is_pure_math(rest: str) -> bool:
    t = rest.lower()
    for p in ("multiplied by", "multiply by", "times", "divided by", "divide by", "plus", "minus", "to the power of", "power of", "modulo", "remainder"):
        t = t.replace(p, " ⊕ ")
    t = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " ⊕ ", t)
    has_op = "⊕" in t or bool(re.search(r"[+\-*/×÷%]", t))
    return has_op and bool(re.search(r"\d", t)) and bool(re.fullmatch(r"[\d\s.+\-*/()×÷%⊕]*", t))


def _quick_local(lower: str) -> Optional[Result]:
    import datetime
    pg = None
    if re.search(r"\bwhat(?:'s| is)?(?: the)? time\b|\bwhat time is it\b|^(?:current )?time$", lower):
        return True, "It's " + datetime.datetime.now().strftime("%I:%M %p").lstrip("0") + "."
    if re.search(r"\bwhat(?:'s| is)?(?: the)?(?: date| day)(?: today)?\b|\btoday'?s date\b|\bwhat day is it\b", lower):
        return True, "Today is " + datetime.datetime.now().strftime("%A, %B %d, %Y").replace(" 0", " ") + "."
    m = re.match(r"^(?:calculate|compute|what(?:'s| is)|how much is|whats)\s+(.+)$", lower)
    if m and _is_pure_math(m.group(1)):
        return _calculate(m.group(1))
    if re.search(r"\bunmute\b|\bmute\b", lower) and "volume" not in lower.replace("mute", ""):
        return _volume("mute")
    # exact / relative volume & brightness: "volume to 50 percent", "brightness max", "a bit louder"
    if re.search(r"\b(?:volume|louder|quieter|sound level)\b", lower) and not re.search(r"\b(?:mute|unmute)\b", lower):
        if levels.parse_level(lower, 50) is not None:
            return levels.adjust("volume", lower)
    if re.search(r"\b(?:brightness|brighter|dimmer|dim the screen)\b", lower):
        if levels.parse_level(lower, 50) is not None:
            return levels.adjust("brightness", lower)
    if re.search(r"\bvolume\b|\blouder\b|\bquieter\b", lower):
        if re.search(r"\bmute\b", lower):
            return _volume("mute")
        if re.search(r"\b(up|increase|raise|higher|louder)\b", lower):
            return _volume("up")
        if re.search(r"\b(down|decrease|lower|reduce|quieter|softer)\b", lower):
            return _volume("down")
    if re.search(r"\bbrightness\b", lower):
        if re.search(r"\b(up|increase|raise|higher|brighter)\b", lower):
            return _brightness("up")
        if re.search(r"\b(down|decrease|lower|reduce|dim|dimmer)\b", lower):
            return _brightness("down")
    if re.search(r"\b(?:too loud|so loud|it'?s loud|hurts my ears|turn it down|lower it|quieter|softer|reduce the sound)\b", lower):
        return _volume("down")
    if re.search(r"\b(?:too quiet|too low|so quiet|can'?t hear|cannot hear|turn it up|louder|raise it|increase the sound)\b", lower):
        return _volume("up")
    if re.search(r"\b(?:too dark|so dark|too dim|can'?t see the screen|screen is dim|brighter|more bright)\b", lower):
        return _brightness("up")
    if re.search(r"\b(?:too bright|so bright|hurts my eyes|dimmer|dim the screen)\b", lower):
        return _brightness("down")
    m = re.fullmatch(r"(?:scroll|go|move|page)(?: the page| page| the screen| screen)?(?: all the way)?(?: to)?(?: the)? (up|down|top|bottom)"
                     r"(?: a (?:little|bit|lot))?(?: a little| a bit| more| again)?(?: please)?", lower)
    if m:
        return _smart_scroll(m.group(1))
    if re.fullmatch(r"(?:scroll|page) ?(up|down)", lower):
        return _smart_scroll(re.fullmatch(r"(?:scroll|page) ?(up|down)", lower).group(1))
    if lower in {"scroll", "scroll more", "keep scrolling", "more", "go further down"}:
        return _smart_scroll("down")
    if lower in {"play", "resume", "play video", "resume video", "play music", "resume music"}:
        return _media_control("play")
    if lower in {"pause", "pause video", "pause music", "stop video", "stop music"}:
        return _media_control("pause")
    if re.search(r"\b(?:take|capture)(?: a)? screenshot\b", lower):
        return _screenshot()
    if re.fullmatch(r"lock (?:the |my )?(?:pc|computer|laptop|screen)", lower):
        return _run_hotkey("lock")
    m = re.fullmatch(r"(?:open|show|go to) (?:the )?(.+?) settings?", lower)
    if m:
        return _settings(m.group(1))
    return None


def _tab_op(name: str, keys: tuple) -> Result:
    """Tab operations: extension first (focus-independent), keyboard shortcut as fallback."""
    nav = {"next": {"to": "next"}, "prev": {"to": "prev"}, "last": {"to": "last"}, "first": {"to": "first"}}
    if _bridge_ready():
        if name in {"new_tab", "close_tab"}:
            ok, payload = _bridge_call(name)
            if ok:
                return True, _speakable(name, payload)
        elif name in nav:
            ok, payload = _bridge_call("goto_tab", **nav[name])
            if ok:
                return True, _speakable("switch_tab", payload)
        elif name == "refresh":
            pass                                           # keyboard is fine for refresh
    pg = _pg()
    if pg is None:
        return False, "Keyboard automation isn't available."
    if not _find_chrome_window():
        return False, "I couldn't find an open Chrome window."
    try:
        pg.hotkey(*keys)
        return True, "Done."
    except Exception as exc:
        return False, f"Browser shortcut failed: {exc}"


def _open_profile_picker() -> None:
    pg = _pg()
    if pg is not None and _find_chrome_window():
        pg.hotkey("ctrl", "l")
        pg.write("chrome://profile-picker", interval=0.01)
        pg.press("enter")


def _choose_profile(n: Optional[int] = None, name: Optional[str] = None) -> Result:
    try:
        import screen_vision
    except Exception as exc:
        return False, f"Screen vision isn't available: {exc}"
    return screen_vision.choose_profile(n=n, name=name, focus=_find_chrome_window, open_picker=_open_profile_picker)


def _browser_control(text: str) -> Optional[Result]:
    """Deterministic tab / site / profile / messaging controls (runs before any AI)."""
    global _pending_action
    lower = re.sub(r"\s+", " ", text.lower()).strip().rstrip(".!?")
    if re.search(r"\b(open|create|make|add) (a |one more |another |new |fresh )*(browser )?tab\b|^new (browser )?tab$", lower) and not re.search(_ORD_RE + r" tab", lower):
        return _tab_op("new_tab", ("ctrl", "t"))
    if re.search(r"\b(close|shut) (this |the |current )?(browser )?tab\b", lower):
        return _tab_op("close_tab", ("ctrl", "w"))
    if re.search(r"\b(reopen|restore) (the )?(last )?closed tab\b", lower):
        return _tab_op("reopen", ("ctrl", "shift", "t"))
    if re.search(r"\b(?:next tab|switch tabs?|change tabs?|switch to (?:the )?next tab|go to (?:the )?next tab)\b", lower):
        return _tab_op("next", ("ctrl", "tab"))
    if re.search(r"\b(?:previous|prior) tab\b|\bswitch back (?:to )?the previous tab\b|\bgo back (?:a |one )?tab\b", lower):
        return _tab_op("prev", ("ctrl", "shift", "tab"))
    if re.search(r"\b(?:last|final) tab\b", lower):
        return _tab_op("last", ("ctrl", "9"))
    if re.search(r"\bfirst tab\b", lower):
        return _tab_op("first", ("ctrl", "1"))
    if lower in {"refresh", "refresh page", "reload", "reload page", "refresh the page", "reload the page", "refresh this page"}:
        return _tab_op("refresh", ("ctrl", "r"))
    if lower in {"open chrome", "open google chrome", "bring up chrome", "switch to chrome", "go to chrome"}:
        return _open_app("Google Chrome")

    # Switch to an already-open tab by (part of) its name.
    m = re.search(r"\b(?:switch to|go to|move to|take me to) (?:the )?(.+?) tab\b", lower)
    if m:
        name = m.group(1).strip()
        if name in {"new", "next", "previous", "last", "first"} or re.fullmatch(_ORD_RE, name):
            return None
        if _bridge_ready():
            ok, payload = _bridge_call("switch_tab", query=name)
            return (True, _speakable("switch_tab", payload)) if ok else (False, str(payload))
        pg = _pg()
        if pg is None or not _find_chrome_window():
            return False, "I couldn't find an open Chrome window."
        try:
            pg.hotkey("ctrl", "shift", "a"); time.sleep(0.4); pg.hotkey("ctrl", "a")
            pg.write(name, interval=0.03); time.sleep(0.6); pg.press("enter")
            return True, f"Searched Chrome's open tabs for {name}. Verify the selected tab if several match."
        except Exception as exc:
            return False, f"Couldn't search Chrome tabs: {exc}"

    # --- Chrome profiles (native window: needs screen vision, the extension can't see it) ---
    m = (re.fullmatch(rf"(?:please )?(?:open|select|choose|pick|use|switch to|go to|click)(?: on)?(?: the)? {_ORD_RE}(?: chrome)? profile", lower)
         or re.fullmatch(rf"(?:please )?(?:open|select|choose|pick|use|switch to|click)(?: on)? (?:chrome )?profile {_ORD_RE}", lower)
         or re.fullmatch(rf"(?:profile|account) {_ORD_RE}", lower))
    if m:
        return _choose_profile(n=_parse_ordinal(m.group(1)))
    m = (re.fullmatch(r"(?:please )?(?:open|select|choose|pick|use|switch to)(?: the)? (?:chrome )?profile (?:named |called )(.+)", lower)
         or re.fullmatch(r"(?:please )?(?:open|select|choose|pick|use|switch to)(?: the)? (?!chrome\b)(.+?)(?:'s)? (?:chrome )?profile", lower))
    if m and "picker" not in m.group(1) and not re.fullmatch(_ORD_RE, m.group(1)):
        return _choose_profile(name=m.group(1).strip())
    if re.search(r"\b(open|show) (the )?(chrome )?(profile picker|profiles|profile list)\b|\bswitch (?:chrome )?profiles?\b", lower):
        pg = _pg()
        if pg is None or not _find_chrome_window():
            return False, "Open Chrome first."
        _open_profile_picker()
        return True, "Opened Chrome's profile picker. Say 'select the third profile' or a profile name."

    # ChatGPT prompt workflow
    m = re.search(r"(?:go to|open|switch to) chatgpt(?: tab)? and (?:open )?(?:a )?new chat and (?:type|write|enter|ask) (.+)$", text, re.I)
    if m:
        pg = _pg()
        if pg is None:
            return False, "Keyboard automation isn't available."
        _navigate_existing_browser("https://chatgpt.com/", "ChatGPT new chat")
        time.sleep(2.5)
        try:
            _clipboard_paste(m.group(1).strip().strip('"'))
            pg.press("enter")
            return True, "Submitted the prompt in ChatGPT; verify it appeared in the intended conversation."
        except Exception as exc:
            return False, f"ChatGPT prompt failed: {exc}"

    # WhatsApp: NEVER send without an explicit confirmation.
    m = re.search(r"(?:send|message|text) (?:a )?(?:whatsapp )?(?:message )?to (.+?) (?:saying|that says|with the message) (.+)$", text, re.I)
    if m:
        contact, message = m.group(1).strip(), m.group(2).strip().strip('"')
        _pending_action = {"action": "send_whatsapp", "contact": contact, "message": message}
        return True, f"I'll send '{message[:120]}' to {contact} on WhatsApp. Say 'confirm' to send it or 'cancel'."
    m = re.search(r"\b(?:open|find|go to) (.+?) chat\b", lower)
    if m and "whatsapp" in lower:
        contact = m.group(1).replace("whatsapp", "").strip()
        pg = _pg()
        if pg is None:
            return False, "Keyboard automation isn't available."
        _navigate_existing_browser("https://web.whatsapp.com", "WhatsApp Web")
        time.sleep(2.5)
        try:
            pg.hotkey("ctrl", "alt", "/"); time.sleep(0.4); pg.write(contact, interval=0.04); time.sleep(0.9); pg.press("enter")
            return True, f"Searched WhatsApp for {contact}. Verify the chat before sending anything."
        except Exception as exc:
            return False, f"WhatsApp chat search failed: {exc}"

    # Named sites; the word "tab" means: find the existing tab instead of loading a new page.
    names = "|".join(sorted((re.escape(k) for k in _SITES), key=len, reverse=True))
    m = re.fullmatch(rf"(?:switch to|move to|go to|take me to|open|show me)(?: the)? ({names})( tab)?", lower)
    if m:
        name, is_tab = m.group(1), bool(m.group(2))
        if is_tab and _bridge_ready():
            ok, payload = _bridge_call("switch_tab", query=name.split()[0])
            if ok:
                return True, _speakable("switch_tab", payload)
            # no such tab open -> just open the site below
        return _navigate_existing_browser(_SITES[name], name)

    m = re.search(r"\b(?:search|find|show me) (.+?) (?:videos|video)\b", lower)
    if m and "youtube" not in lower:
        return _navigate_existing_browser("https://www.google.com/search?" + urllib.parse.urlencode({"q": m.group(1), "tbm": "vid"}), "video search")
    m = re.search(r"\b(?:search|find|show me) (.+?) images?\b", lower)
    if m:
        return _navigate_existing_browser("https://www.google.com/search?" + urllib.parse.urlencode({"q": m.group(1), "tbm": "isch"}), "image search")
    return None


def _page_commands(text: str, lower: str) -> Optional[Result]:
    """Commands that act on the page the user is looking at (clicks, reading, in-page search, YouTube)."""
    # --- media: pause / resume / next / skip / speed / fullscreen of whatever is playing ---
    if re.fullmatch(r"(?:play|resume|continue|unpause)(?: it| that| this| the (?:video|music|song|audio|track|playback))?(?: again)?", lower):
        return _media_control("play")
    if re.fullmatch(r"(?:pause|hold|freeze)(?: it| that| this| the (?:video|music|song|audio|track|playback))?", lower):
        return _media_control("pause")
    if re.fullmatch(r"(?:skip|skip this|skip it|next|play next|(?:go to |skip to |play )?(?:the )?next (?:video|song|track|one))", lower):
        return _media_control("next")
    if re.fullmatch(r"(?:previous|play previous|(?:go to |play |skip to )?(?:the )?(?:previous|last|prior) (?:video|song|track|one))", lower):
        return _media_control("previous")
    m = re.fullmatch(r"(?:skip|fast forward|forward|go forward|jump)(?: ahead| forward)?(?: by)?(?: (\d{1,3}) seconds?)?", lower)
    if m and (m.group(1) or "forward" in lower or "ahead" in lower):
        return _media_control("skip_ahead", int(m.group(1) or 10))
    m = re.fullmatch(r"(?:rewind|go back|skip back|back)(?: by)?(?: (\d{1,3}) seconds?)?", lower)
    if m and (m.group(1) or lower.startswith("rewind") or "skip" in lower):
        return _media_control("skip_back", int(m.group(1) or 10))
    if re.fullmatch(r"(?:speed up|faster|play faster|increase (?:the )?speed)", lower):
        return _media_control("faster")
    if re.fullmatch(r"(?:slow down|slower|play slower|decrease (?:the )?speed)", lower):
        return _media_control("slower")
    if re.fullmatch(r"(?:full ?screen|go full ?screen|make it full ?screen|exit full ?screen|leave full ?screen)", lower):
        return _media_control("fullscreen")
    if re.fullmatch(r"(?:go back|back|go to the previous page|previous page)", lower):
        return _browser_nav("back")
    if re.fullmatch(r"(?:go forward|forward|next page)", lower):
        return _browser_nav("forward")
    if re.fullmatch(r"zoom (in|out)|(?:make (?:it|the page|the text) )?(bigger|smaller)|reset zoom", lower):
        m2 = re.fullmatch(r"zoom (in|out)|(?:make (?:it|the page|the text) )?(bigger|smaller)|reset zoom", lower)
        op = "reset" if lower == "reset zoom" else {"in": "in", "out": "out", "bigger": "in", "smaller": "out"}[m2.group(1) or m2.group(2)]
        return _browser_nav("zoom", op=op)
    # a bare "second one" / "the 3rd" / "open number 2" while Chrome is in front means the 2nd/3rd result
    m = re.fullmatch(rf"(?:please )?(?:(?:open|play|click(?: on)?|select|choose|watch|go to|take me to|show(?: me)?|pick|tap)\s+)?(?:the )?{_ORD_RE}"
                     rf"(?: one| result| video| link| website| site| song| item| option)?(?: please)?", lower)
    if m and (m.group(1) in _PLAIN_ORDINALS or re.match(r"\d+(?:st|nd|rd|th)$|number \d+$", m.group(1))) \
            and _bridge_ready() and _is_chrome_title(_foreground_title()):
        return _click_nth(_parse_ordinal(m.group(1)), "result")
    # "open the second video / play the third result / click the first link"
    m = re.fullmatch(rf"(?:please )?(?:open|play|click(?: on)?|select|choose|watch|go to|show)(?: me)?(?: the)? {_ORD_RE}(?: one)?(?: {_KIND_RE})?", lower)
    if m:
        tok, kind = m.group(1), m.group(2)
        if kind or tok in _PLAIN_ORDINALS:
            return _click_nth(_parse_ordinal(tok), {"link": "link", "tab": "tab"}.get(kind or "", "result"))

    m = re.search(rf"\b(?:open|play|click(?: on)?|select|choose|watch|go to|take me to|show(?: me)?|pick|tap|press|visit)\b.*?\b{_ORD_RE}\b(?: one)? ?{_KIND_RE}\b", lower)
    if m and not re.search(r"\bprofile\b", lower):
        return _click_nth(_parse_ordinal(m.group(1)), {"link": "link", "tab": "tab"}.get(m.group(2), "result"))

    if re.search(r"\b(?:what(?:'s| is) (?:on )?(?:this|the current) (?:page|tab)|which page am i on|what page is this|read (?:this|the)(?: current)? page|what(?:'s| is) open)\b", lower):
        ok, state = _page_snapshot()
        return (True, _describe_page(state)) if ok else (False, str(state))
    if re.search(r"\b(?:summari[sz]e|sum up|give me (?:a )?summary of)(?: this| the current| the)?(?: page| tab| article| website)?\b", lower) and re.search(r"page|tab|article|website|this", lower):
        ok, state = _page_snapshot()
        return _summarize_page(state) if ok else (False, str(state))

    # "what's on my screen" / "what am I looking at" / "explain this error" -> look at the real screen
    if re.search(r"\b(?:what(?:'s| is) (?:on )?(?:my |the )?screen|what am i (?:looking at|seeing)|what do you see|look at (?:my |the )?screen|read (?:my |the )?screen|describe (?:my |the )?screen)\b", lower) \
            or re.fullmatch(r"(?:explain|what(?:'s| is)) (?:this|that)(?: (?:error|message|screen|window|popup|dialog))?", lower):
        return _screen_describe(text)

    # "search for youtube" / "open youtube" style: the thing named IS a website -> go there
    m = re.fullmatch(r"(?:search|look up|look for|find|go to|open)(?: for)?(?: the)? (.+?)(?: website| site)?", lower)
    if m and m.group(1).strip() in _SITES:
        return _navigate_existing_browser(_SITES[m.group(1).strip()], m.group(1).strip())

    # Context-aware search: if the user is ALREADY on YouTube, "search for X" means search YouTube.
    m = re.fullmatch(r"(?:search|look up|look for|find)(?: for)? (.+)", lower)
    if m and not re.search(r"\b(?:images?|videos?)\b$", m.group(1)):
        host = _current_host()
        if host.endswith("youtube.com"):
            return _youtube(m.group(1).strip(), play=False)

    # YouTube
    m = (re.fullmatch(r"(?:play|watch|put on) (.+?) (?:on|in|from) youtube", lower)
         or re.fullmatch(r"youtube play (.+)", lower))
    if m:
        return _youtube(m.group(1).strip(), play=True)
    m = (re.fullmatch(r"(?:search|find|look up|look for)(?: for)? (.+?) (?:on|in) youtube", lower)
         or re.fullmatch(r"(?:search )?youtube(?: search)?(?: for)? (.+)", lower)
         or re.fullmatch(r"search youtube for (.+)", lower))
    if m:
        return _youtube(m.group(1).strip(), play=False)
    m = re.fullmatch(r"(?:play|listen to|put on|stream)(?: me)? (?!the (?:next|previous)|next|previous)(.+)", lower)
    if m and not re.fullmatch(_ORD_RE + r".*", m.group(1)) and len(m.group(1).split()) <= 10:
        what = m.group(1).strip()
        if what in {"something", "some music", "a song", "music", "songs", "a video", "something good", "anything", "it", "that", "this"}:
            return None                                       # too vague: let the brain pick or ask
        return _youtube(what, play=True)                      # "play <song/video>" defaults to YouTube

    # type into the page's search box
    m = re.fullmatch(r"(?:search|type|enter|write) (.+?) (?:in|into|on) (?:the |this )?(?:search ?(?:box|bar|field)|page)", lower)
    if m:
        return _bridge_result("search", query=m.group(1).strip(), hint="search", submit=True)

    # click a named button/link: the page if Chrome is in front, otherwise (or if not found) whatever is on the real screen
    m = (re.fullmatch(r"(?:click|tap|hit|press)(?: on)? (?:the )?(.+?) (?:button|link|icon)", lower)
         or re.fullmatch(r"(?:click|tap)(?: on)? (?:the )?(.+)", lower))
    if m and m.group(1) not in {"enter", "tab", "escape", "space", "here"}:
        return _click_named(m.group(1).strip())

    # plain web search
    m = re.fullmatch(r"(?:search(?: the web| google)?(?: for)?|google|look up|look for) (.+)", lower)
    if m:
        return _execute({"action": "web_search", "query": m.group(1).strip()})

    # open <thing>: known website / folder / app (refuse vague phrases so we never type nonsense into Start)
    m = re.fullmatch(r"(?:open|launch|start|run)(?: the)?(?: app)? (.+)", lower)
    if m:
        target = re.sub(r"\s+(folder|app|application|program|website|site)$", "", m.group(1).strip())
        words = target.split()
        if target in _SITES:
            return _navigate_existing_browser(_SITES[target], target)
        if target in _FOLDERS:
            return _open_folder(target)
        if 0 < len(words) <= 3 and not (set(words) & _NOT_AN_APP) and not re.match(_ORD_RE + r"\b", target) \
                and not re.search(r"\b(?:about|for|with|that|which|where|of)\b", target):
            if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?", target):
                return _open_url(target, target)
            return _open_app(target)
    return None


# =====================================================================
#  MEDIA + BROWSER NAVIGATION HELPERS
# =====================================================================
_MEDIA_SAY = {"play": "Playing.", "pause": "Paused.", "toggle": "Toggled playback.", "mute": "Muted.", "unmute": "Unmuted.",
              "next": "Skipped to the next one.", "previous": "Went to the previous one.", "skip_ahead": "Skipped ahead.",
              "skip_back": "Skipped back.", "faster": "Playing faster.", "slower": "Playing slower.", "fullscreen": "Toggled full screen."}
_MEDIA_KEYS = {"play": "playpause", "pause": "playpause", "toggle": "playpause", "next": "nexttrack", "previous": "prevtrack",
               "mute": "volumemute", "unmute": "volumemute"}


def _media_control(op: str, seconds=None) -> Result:
    """Control the video/music in the active Chrome tab; fall back to system-wide media keys (Spotify etc.)."""
    err = ""
    if _bridge_ready():
        kw = {"op": op}
        if seconds:
            kw["seconds"] = int(seconds)
        ok, payload = _bridge_call("media", **kw)
        if ok:
            return True, _MEDIA_SAY.get(op, "Done.")
        err = str(payload)
        log.info("browser media %s failed (%s)", op, err)
    pg = _pg()
    if pg is None:
        return False, err or "Keyboard automation isn't available."
    try:
        if op == "fullscreen" or "needs_keyboard" in err:
            if not _find_chrome_window():
                return False, "I couldn't bring Chrome to the front."
            pg.press("f")
            return True, "Toggled full screen."
        key = _MEDIA_KEYS.get(op)
        if key:
            pg.press(key)
            return True, _MEDIA_SAY.get(op, "Done.")
    except Exception as exc:
        return False, f"Media control failed: {exc}"
    return False, err or f"I can't do that without a video playing in Chrome."


def _browser_nav(action: str, **kw) -> Result:
    """back / forward / reload / zoom: Chrome API first, keyboard second."""
    say = {"back": "Went back.", "forward": "Went forward.", "reload": "Reloaded the page.", "zoom": "Adjusted the zoom."}
    if _bridge_ready():
        ok, payload = _bridge_call(action, **kw)
        if ok:
            if action == "zoom" and isinstance(payload, dict) and payload.get("zoom"):
                return True, f"Zoom is now {payload['zoom']} percent."
            return True, say[action]
        log.warning("bridge %s failed: %s", action, payload)
    pg = _pg()
    if pg is None or not _find_chrome_window():
        return False, "I couldn't reach Chrome for that."
    keys = {"back": ("alt", "left"), "forward": ("alt", "right"), "reload": ("f5",),
            "zoom": ("ctrl", {"in": "=", "out": "-", "reset": "0"}.get(kw.get("op", "in"), "="))}[action]
    pg.hotkey(*keys)
    return True, say[action]


def _click_named(query: str, kind: str = "click") -> Result:
    """Click something by its visible name: the web page (if Chrome is in front), otherwise / failing that, the screen."""
    if kind == "click" and _bridge_ready() and _chrome_in_front():
        for act in ("click_text", "click_button"):
            ok, payload = _bridge_call(act, query=query, timeout=8)
            if ok:
                return True, _speakable(act, payload)
        log.info("page click for %r failed; trying the screen", query)
    return _screen_click(query, kind)


# =====================================================================
#  UNDERSTANDING: speech clean-up, context, memory
# =====================================================================
_ASR_FIXES = [
    (r"\b(?:you ?tube|u ?tube)\b", "youtube"), (r"\bg ?mail\b|\bgee mail\b", "gmail"),
    (r"\bwhats ?app\b|\bwhat's app\b|\bwatsapp\b", "whatsapp"), (r"\bchat ?gpt\b|\bchat g p t\b", "chatgpt"),
    (r"\blinked in\b", "linkedin"), (r"\b(?:crome|krome|chrom|chrome's)\b", "chrome"),
    (r"\b(new|next|previous|last|first|close|this|current|that|another|a|the) (?:tap|tub|tabs|tab's)\b", r"\1 tab"),
    (r"\b(new|close|next|previous|this|current) top\b(?! of)", r"\1 tab"),
    (r"\b(second|third|fourth|fifth|sixth|\d+(?:st|nd|rd|th)?) (?:tap|tub|tabs)\b", r"\1 tab"),
    (r"\bnew tab(?:s)? tab\b", "new tab"), (r"\bwindows? setting\b", "windows settings"),
    (r"\bsong's\b", "songs"),
    (r"\bgit ?hub\b|\bget hub\b", "github"), (r"\bper ?cent\b|\bpercentage\b", "percent"),
    (r"\bbright ?ness\b", "brightness"), (r"\bcurrent windows\b", "current window"),
    (r"\bscrawl\b|\bscroll's\b", "scroll"), (r"\bclaud\b|\bclawed\b|\bcloud (?=window|app)\b", "claude"),
]
_PREFIX = re.compile(
    r"^(?:(?:so|well|um+|uh+|hmm+|please|kindly|just|now|actually|then|and|also|okay|ok|"
    r"can you|could you|would you|will you|can u|could u|do you mind|i want you to|i want to|i wanna|i would like you to|"
    r"i would like to|i'd like to|i need you to|i need to|let's|lets|go ahead and|try to|you can|you should|"
    r"i want|i need|vaani|computer|hey)\b[ ,]*)+", re.I)
_SUFFIX = re.compile(r"[ ,]*\b(?:please|for me|right now|now|thanks|thank you|if you can|okay|ok)\s*[.!?]*$", re.I)


def _fix_asr(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    for pattern, repl in _ASR_FIXES:
        t = re.sub(pattern, repl, t, flags=re.I)
    return t


def _normalize_command(text: str) -> str:
    """Strip politeness/filler so 'could you please open a new tab for me' matches the fast rules."""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"^\s*(?:hey|hi|ok|okay)[\s,.-]+(?:computer|vaani)[\s,.:;-]*", "", t, flags=re.I)
    original = t.strip(" ,.!?")
    for _ in range(2):
        t = _PREFIX.sub("", t).strip()
        t = _SUFFIX.sub("", t).strip()
    return t.strip(" ,.!?") or original            # never reduce "thanks" / "okay" to nothing


_CHIT_CHAT = {"hello", "hi", "hey", "thanks", "thank you", "good morning", "good evening", "good afternoon", "good night",
              "how are you", "who are you", "what is your name", "what's your name", "whats your name", "bye", "goodbye",
              "nice", "great", "awesome", "cool", "okay", "ok", "yes", "no", "i love you", "you are awesome", "tell me a joke",
              "what can you do", "help"}
_QSTART = re.compile(r"^(?:what|what's|whats|who|who's|whom|whose|why|how|when|where|which|is|are|was|were|do you|do i|do we|does|did you|"
                     r"tell me|explain|define|describe|meaning of|translate|give me (?:a|an|some) (?:idea|tip|fact|summary))\b")
_ACTIONISH = re.compile(r"\b(open|play|search|click|tab|tabs|volume|brightness|screen|page|video|videos|song|songs|music|close|switch|"
                        r"scroll|type|zoom|pause|mute|go to|go back|reload|refresh|window|app|settings|screenshot|whatsapp|youtube|"
                        r"chrome|website|link|result|results|button|profile|louder|quieter|next|previous|skip|fullscreen|full screen|"
                        r"desktop|folder|download|install|lock|shutdown|restart|this error|on my screen)\b")


def _is_plain_chat(lower: str) -> bool:
    """Obvious conversation / knowledge questions skip the planner (saves a network call)."""
    l = lower.strip(" .!?")
    if l in _CHIT_CHAT:
        return True
    return bool(_QSTART.match(l)) and not _ACTIONISH.search(l)


_brain_notice_at = -1e9
_pending_ask: Optional[dict] = None          # a clarifying question VAANI just asked
_recent: deque = deque(maxlen=4)                       # what Chief asked and what happened (for "do that again", "the next one")
_PAGEISH = re.compile(r"\b(click|link|links|result|results|video|videos|song|songs|music|first|second|third|fourth|fifth|one|button|"
                      r"page|scroll|type|fill|box|field|pause|play|resume|next|previous|skip|mute|unmute|speed|faster|slower|"
                      r"fullscreen|full|this|that|here|read|summar\w*|stop|louder|quieter)\b", re.I)


def _remember(user: str, steps: list, outcome: str) -> None:
    did = ", ".join(f"{s['do']}" + (f":{s.get('query') or s.get('name') or s.get('index') or s.get('op') or ''}" if len(s) > 1 else "")
                    for s in steps)[:140]
    _recent.append({"chief": user[:140], "did": did, "result": (outcome or "")[:140]})


def _slim_page(state: dict, want_text: bool) -> dict:
    def txt(x, n):
        return str(x.get("text") if isinstance(x, dict) else x or "")[:n]

    def idx(x):
        return x.get("index") if isinstance(x, dict) else None
    out = {"title": (state.get("title") or "")[:100], "url": (state.get("url") or "")[:160]}
    if state.get("media"):
        out["media"] = state["media"]
    res = state.get("results") or []
    if res:
        out["results"] = [{"i": idx(r), "t": txt(r, 80)} for r in res[:10]]
    else:
        out["links"] = [{"i": idx(r), "t": txt(r, 60)} for r in (state.get("links") or [])[:12]]
    out["buttons"] = [txt(b, 40) for b in (state.get("buttons") or [])[:15]]
    out["inputs"] = [str((i.get("placeholder") or i.get("aria") or i.get("name") or i.get("tag")) if isinstance(i, dict) else i)[:40]
                     for i in (state.get("inputs") or [])[:5]]
    if want_text:
        out["text"] = (state.get("text") or "")[:1500]
    return out


def _gather_context(text: str) -> dict:
    """What Chief is looking at right now: foreground window, current tab, (if relevant) page details, recent exchanges."""
    import datetime
    ctx: dict = {"now": datetime.datetime.now().strftime("%A %I:%M %p")}
    try:
        import longmem
        mem = longmem.context_for(text)
        if mem:
            ctx["memory"] = mem
    except Exception:
        pass
    fg = _foreground_title()
    if fg:
        ctx["window"] = fg[:120]
        ctx["chrome_in_front"] = _is_chrome_title(fg)
    if _bridge_ready():
        browser: dict = {"connected": True}
        if _PAGEISH.search(text):
            ok, state = _page_snapshot()
            if ok and state:
                ctx["page"] = _slim_page(state, bool(re.search(r"\b(read|summar\w*|about|say|says|mean|what)\b", text, re.I)))
                browser.update(url=ctx["page"]["url"], title=ctx["page"]["title"], media=state.get("media"))
        if "url" not in browser:
            ok, payload = _bridge_call("ping", timeout=3)
            tab = (payload or {}).get("activeTab") if ok and isinstance(payload, dict) else None
            if tab:
                browser.update(url=(tab.get("url") or "")[:160], title=(tab.get("title") or "")[:100])
        ctx["browser"] = browser
    else:
        ctx["browser"] = {"connected": False}
    if _recent:
        ctx["recent"] = list(_recent)
    return ctx


# =====================================================================
#  EXECUTING A PLAN (every step is allow-listed by gemini_planner.validate_steps)
# =====================================================================
def _resolve_site(query: str):
    low = re.sub(r"^(?:the )?(.+?)(?: website| site| page| app)?$", r"\1", query.strip().lower())
    if low in _SITES:
        return _SITES[low], low
    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?", low):
        return "https://" + low, low
    return "https://www.google.com/search?btnI=1&q=" + urllib.parse.quote_plus(low + " official site"), low


def _announce(text: str) -> None:
    """Say a short progress line before slow work (screen reading), so Chief knows VAANI heard and is busy."""
    if os.getenv("VAANI_QUIET"):
        return
    try:
        import voice
        voice.speak(text)
    except Exception:
        log.exception("announce failed")


def _exec_step(st: dict, ctx: dict) -> Result:
    do = st["do"]
    if do in {"look", "screen_task"}:
        _announce("One moment, Chief. I'm looking at your screen." if do == "look" else "On it, Chief.")
    chrome_front = ctx.get("chrome_in_front", True)
    pg = None
    # ---- browser ----
    if do == "new_tab":
        if _bridge_ready():
            ok, payload = _bridge_call("new_tab", **({"url": st["url"]} if st.get("url") else {}))
            if ok:
                return True, "Opened a new tab."
        r = _tab_op("new_tab", ("ctrl", "t"))
        return r if not (r[0] and st.get("url")) else _navigate_existing_browser(st["url"], st["url"])
    if do == "close_tab":
        return _tab_op("close_tab", ("ctrl", "w"))
    if do == "goto_tab":
        if st.get("index"):
            return _click_nth(st["index"], "tab")
        return _tab_op({"next": "next", "prev": "prev", "first": "first", "last": "last"}[st["to"]],
                       {"next": ("ctrl", "tab"), "prev": ("ctrl", "shift", "tab"), "first": ("ctrl", "1"), "last": ("ctrl", "9")}[st["to"]])
    if do == "switch_tab":
        if not _bridge_ready():
            return False, _NOT_CONNECTED
        ok, payload = _bridge_call("switch_tab", query=st["query"])
        return (True, _speakable("switch_tab", payload)) if ok else (False, str(payload))
    if do == "open_site":
        if st.get("url"):
            return _navigate_existing_browser(st["url"], st.get("query") or st["url"])
        url, label = _resolve_site(st["query"])
        return _navigate_existing_browser(url, label)
    if do == "search_web":
        return _navigate_existing_browser("https://www.google.com/search?q=" + urllib.parse.quote_plus(st["query"]),
                                          f"a web search for {st['query']}")
    if do == "search_youtube":
        return _youtube(st["query"], play=False)
    if do == "play_youtube":
        return _youtube(st["query"], play=True)
    if do == "click_result":
        return _click_nth(st["index"], "result")
    if do == "click_link":
        return _click_nth(st["index"], "link")
    if do == "click":
        return _click_named(st["query"])
    if do == "type":
        if _bridge_ready() and chrome_front:
            ok, payload = _bridge_call("type", text=st["text"], hint=st.get("field") or "search", submit=bool(st.get("submit")))
            if ok:
                return True, _speakable("type", payload)
            log.info("page typing failed (%s); typing on the keyboard instead", payload)
        r = _clipboard_paste(st["text"])
        if r[0] and st.get("submit"):
            _press_key("enter")
        return r
    if do == "scroll":
        return _smart_scroll(st.get("direction", "down"))
    if do == "back":
        return _browser_nav("back")
    if do == "forward":
        return _browser_nav("forward")
    if do == "reload":
        return _browser_nav("reload")
    if do == "zoom":
        return _browser_nav("zoom", op=st["op"])
    if do == "media":
        return _media_control(st["op"], st.get("seconds"))
    if do == "read_page":
        ok, state = _page_snapshot()
        return (True, _describe_page(state)) if ok else (False, str(state))
    if do == "summarize_page":
        ok, state = _page_snapshot()
        return _summarize_page(state) if ok else (False, str(state))
    # ---- computer ----
    if do == "open_app":
        if _looks_like_page_reference(st["name"]):
            return False, f"'{st['name']}' sounds like something on the page, not an app, so I didn't open it."
        return _open_app(st["name"])
    if do == "close_window":
        return _execute({"action": "close_window"})
    if do == "open_folder":
        return _open_folder(st["name"])
    if do == "open_settings":
        return _settings(st["name"])
    if do in {"volume", "brightness"}:
        op = st["op"]
        if do == "volume" and op in {"mute", "unmute"}:
            return _volume("mute")
        if op in {"max", "min"}:
            return levels.adjust(do, f"{do} {op}")
        if st.get("level") is not None:
            return levels.adjust(do, f"{do} to {int(st['level'])} percent")
        if st.get("amount") is not None:
            return levels.adjust(do, f"{do} {op} by {int(st['amount'])}")
        return levels.adjust(do, f"{do} {op}")
    if do == "screenshot":
        return _screenshot()
    if do == "hotkey":
        return _run_hotkey(st["name"])
    if do == "press":
        return _press_key(st["key"])
    if do == "type_text":
        return _clipboard_paste(st["text"])
    if do == "media_key":
        return _media_system_key(st["op"])
    # ---- screen ----
    if do == "look":
        return _screen_describe(st.get("question") or "")
    if do == "screen_click":
        return _screen_click(st["target"], {"double": "double", "right": "right"}.get(st.get("kind", ""), "click"))
    if do == "screen_task":
        global _pending_action
        try:
            import desktop_agent
            risky = bool(desktop_agent.RISKY.search(st["goal"]))
        except Exception as exc:
            return False, f"The screen agent isn't available: {exc}"
        if risky:
            _pending_action = {"action": "screen_task", "goal": st["goal"]}
            return True, f"I'm about to do this on your screen: {st['goal']}. That could change or send something. Say confirm to go ahead, or cancel."
        return desktop_agent.run(st["goal"], max_steps=10, announce=None)
    if do == "window":
        import winmgr
        op_map = {"snap_left": ("snap", "left"), "snap_right": ("snap", "right"), "snap_top": ("snaptb", "top"), "snap_bottom": ("snaptb", "bottom"),
                  "maximize": ("maximize", ""), "restore": ("restore", ""), "minimize": ("minimize", ""), "show_desktop": ("desktop", ""),
                  "next_monitor": ("monitor", "next"), "prev_monitor": ("monitor", "previous"), "next_desktop": ("desktopswitch", "next"),
                  "prev_desktop": ("desktopswitch", "previous"), "new_desktop": ("newdesktop", ""), "task_view": ("taskview", "")}
        return winmgr.do(*op_map[st["op"]])
    if do == "timer":
        import timers
        timers.add(int(st["seconds"]), st.get("text") or "timer", "reminder" if st.get("text") else "timer")
        return True, f"Okay, I'll let you know in {timers._say_duration(int(st['seconds']))}."
    if do == "mcp":
        import mcp_client
        if mcp_client.needs_confirm(st["server"], st["tool"]):
            _pending_action = {"action": "mcp", "server": st["server"], "tool": st["tool"], "args": st.get("args") or {}}
            return True, f"I'm about to use {st['tool']}. Say confirm to go ahead, or cancel."
        return mcp_client.call(st["server"], st["tool"], st.get("args") or {})
    # ---- other ----
    if do == "time":
        return _quick_local("what time is it") or (False, "I couldn't read the clock.")
    if do == "date":
        return _quick_local("what is the date") or (False, "I couldn't read the date.")
    if do == "calc":
        return _calculate(st["expression"])
    if do == "send_whatsapp":
        _pending_action = {"action": "send_whatsapp", "contact": st["contact"], "message": st["message"]}
        return True, f"I'll send '{st['message'][:120]}' to {st['contact']} on WhatsApp. Say 'confirm' to send it or 'cancel'."
    return False, f"I don't know how to do {do} yet."


def _media_system_key(op: str) -> Result:
    pg = _pg()
    if pg is None:
        return False, "Keyboard automation isn't available."
    pg.press({"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack"}[op])
    return True, {"play_pause": "Toggled playback.", "next": "Skipped to the next track.", "previous": "Went to the previous track."}[op]


_SETTLE = {"new_tab": 0.5, "open_app": 2.5, "open_site": 0.8, "search_web": 0.8, "goto_tab": 0.3, "switch_tab": 0.3,
           "click": 0.9, "click_result": 1.2, "click_link": 1.2, "back": 0.8, "forward": 0.8, "play_youtube": 1.0}


def _run_steps(steps: list, ctx: dict) -> Result:
    msgs = []
    for i, st in enumerate(steps):
        try:
            ok, msg = _exec_step(st, ctx)
        except Exception as exc:
            log.exception("step %s crashed", st)
            return False, f"Something went wrong while doing that: {exc}"
        log.info("step %d/%d %s -> %s %s", i + 1, len(steps), st, ok, msg)
        msgs.append(msg)
        if not ok:
            return False, " ".join(msgs[-2:])[:300]
        if i < len(steps) - 1:
            time.sleep(_SETTLE.get(st["do"], 0.3))
    return True, msgs[0] if len(msgs) == 1 else " ".join(msgs[-2:])[:300]


def _brain_route(text: str):
    """-> ("done", result) | ("chat", None) | ("failed", None). 'failed' means: use the offline fallback."""
    global _pending_ask
    try:
        import gemini_planner as gp
    except Exception as exc:
        log.warning("brain module unavailable: %s", exc)
        return "failed", None
    using_gemini = gp.available()
    planner = gp
    if not using_gemini:                       # no Gemini key: plan with the local qwen model through Ollama
        try:
            import local_brain as planner
        except Exception as exc:
            log.warning("local brain unavailable: %s", exc)
            return "failed", None
        if not planner.available():
            return "failed", None
    started = time.monotonic()
    ctx = _gather_context(text)
    steps, ask, err = planner.plan_steps(text, ctx)
    if err:
        log.warning("brain failed for %r: %s", text, err)
        global _brain_notice_at
        if using_gemini and time.monotonic() - _brain_notice_at > 900:    # at most once every 15 minutes
            _brain_notice_at = time.monotonic()
            _announce("Chief, I can't reach my Gemini brain right now, so I'm using simpler offline mode. Please check the API key.")
        return "failed", None
    if ask and not steps:
        _remember(text, [], f"I asked: {ask}")
        _pending_ask = {"orig": text, "q": ask, "t": time.monotonic()}      # Chief's next sentence answers this question
        return "done", (True, ask)
    if len(steps) == 1 and steps[0]["do"] == "chat":
        return "chat", None
    result = _run_steps(steps, ctx)
    _remember(text, steps, result[1])
    log.info("brain route %r -> %s in %.0f ms", text, result, (time.monotonic() - started) * 1000)
    return "done", result


# =====================================================================
#  ENTRY POINT
# =====================================================================
_COMMAND_VERBS = {
    "open", "close", "launch", "start", "run", "play", "pause", "search", "google", "find", "go", "switch", "minimize",
    "maximize", "scroll", "volume", "brightness", "mute", "unmute", "type", "click", "press", "lock", "screenshot", "take",
    "turn", "increase", "decrease", "raise", "lower", "set", "show", "send", "delete", "erase", "clear", "copy", "paste",
    "select", "save", "undo", "redo", "refresh", "reload", "bring", "move", "enter", "tab", "settings", "folder",
}


def _looks_like_command(lower: str) -> bool:
    return any(w in _COMMAND_VERBS for w in re.findall(r"[a-z']+", lower))


_PAGE_WORDS = re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|1st|2nd|3rd|4th|5th|link|links|result|results|"
                         r"video|videos|website|webpage|page|tab|tabs|button|option|item|song|this one|that one|the one)\b", re.I)


def _looks_like_page_reference(target: str) -> bool:
    """'first link', 'the second video', 'that one' are things ON the page, never application names."""
    return bool(_PAGE_WORDS.search(target or ""))


def _perform_single(orig: str, norm: str | None = None) -> Optional[Result]:
    """Order: confirmations -> instant rules (no AI) -> plain chat check -> brain (Gemini, context aware)
    -> offline fallback (Ollama classifier) -> None (conversation)."""
    norm = norm if norm is not None else _normalize_command(orig)
    lower = re.sub(r"\s+", " ", norm.lower()).strip().rstrip(".!?,")
    started = time.monotonic()
    try:
        confirmation = _confirm_or_cancel(norm)
        if confirmation is not None:
            return confirmation

        for stage, fn in (("quick", lambda: _quick_local(lower)),
                          ("browser", lambda: _browser_control(norm)),
                          ("page", lambda: _page_commands(norm, lower))):
            result = fn()
            if result is not None:
                log.info("route=%s %r -> %s (%.0f ms)", stage, orig, result, (time.monotonic() - started) * 1000)
                return result

        if re.fullmatch(r"(?:close|quit|exit|kill)(?: out of)?(?: the| this| that| my)?(?: current| active| front| foreground| open)?"
                        r"(?: window| app| application| program| one)(?: please| now)?", lower) or lower in {"close notepad", "close the notepad"}:
            return _execute({"action": "close_window"})
        if lower in {"next line", "new line", "go to next line", "move to next line",
                     "take the cursor to the next line", "press enter", "press return"}:
            return _press_key("enter")

        if _is_plain_chat(lower):
            return None

        status, result = _brain_route(orig)
        if status == "done":
            return result
        if status == "chat":
            return None

        # brain unavailable (no key / offline / error): the local model still handles simple computer actions
        if not _looks_like_command(lower):
            return None
        plan = _classify_with_ollama(orig)
        if plan is None:
            return None
        log.info("route=ollama %r -> %s", orig, plan)
        if plan.get("action") in {"open_app", "open_website", "open_folder"} and _looks_like_page_reference(str(plan.get("target") or plan.get("query") or "")):
            log.warning("offline fallback tried to open %r as an app - refused", plan)
            return False, "I'm not sure what you meant, and my smart connection isn't available right now. Could you say it another way?"
        return _execute(plan)
    except Exception as exc:                              # one bad handler must never kill the voice loop
        log.exception("perform_action crashed for %r", orig)
        return False, f"Something went wrong while doing that: {exc}"


# ---------------------------------------------------------------------
#  Multi-step commands: "open Chrome and select the third profile",
#  "go to youtube then play python tutorials"
# ---------------------------------------------------------------------
_STEP_SPLIT = re.compile(
    r"\s*,?\s*\b(?:and then|then|and)\b\s+(?=(?:please\s+)?(?:open|play|search|click|switch|go|close|scroll|select|choose|pick|take|press|use|show|read|summari[sz]e)\b)",
    re.I)


def _split_steps(text: str) -> list:
    low = text.lower().strip()
    if re.match(r"(?:type|write|say|send|message|text)\b", low) or re.search(r"\b(?:saying|that says|with the message)\b", low):
        return [text]                                      # never split text that is meant to be typed/sent
    parts = [p.strip(" ,.") for p in _STEP_SPLIT.split(text) if p and p.strip(" ,.")]
    return parts if len(parts) > 1 else [text]


_INFO_PREFIXES = ("it's ", "it is ", "today is", "the answer", "you're on", "you are on", "i can see", "i see")
_CLOSERS = ("What's next?", "Anything else?", "What should I do next?", "What's next?")
_closer_i = 0


def _style_reply(result: Optional[Result]) -> Optional[Result]:
    """Make replies sound like a person: 'Opened a new tab, Chief. What's next?' (never touches questions or confirmations)."""
    global _closer_i
    if result is None:
        return None
    ok, msg = result
    msg = (msg or "").strip()
    low = msg.lower()
    if not msg or "chief" in low or msg.endswith("?") or "confirm" in low:
        return result
    if not ok:
        return ok, "Sorry, Chief. " + msg
    m = re.search(r"[.!](?=\s)", msg)
    styled = (msg[:m.start()] + ", Chief" + msg[m.start():]) if m else msg.rstrip(".!") + ", Chief."
    _closer_i += 1
    if len(msg) <= 70 and not low.startswith(_INFO_PREFIXES) and _closer_i % 3 == 0:     # not after every single action
        styled += " " + _CLOSERS[(_closer_i // 3) % len(_CLOSERS)]
    return ok, styled


def perform_action(user_text: str) -> Optional[Result]:
    global _pending_ask
    orig = _fix_asr((user_text or "").strip())
    if _pending_ask is not None:
        pend, _pending_ask = _pending_ask, None
        words = re.findall(r"[a-z0-9']+", orig.lower())
        if time.monotonic() - pend["t"] < 45 and 0 < len(words) <= 8 and not _looks_like_command(" ".join(words)):
            # a short answer to the question VAANI just asked -> plan again with both sentences together
            combined = f'{pend["orig"]} (I asked: "{pend["q"]}" and Chief answered: "{orig}")'
            status, result = _brain_route(combined)
            if status == "done":
                return _style_reply(result)
    norm = _normalize_command(orig)
    steps = _split_steps(norm)
    if len(steps) == 1:
        return _style_reply(_perform_single(orig, norm))
    messages = []
    for i, step in enumerate(steps, 1):
        result = _perform_single(step, step)
        log.info("step %d/%d %r -> %s", i, len(steps), step, result)
        if result is None:
            messages.append(f"I didn't understand '{step}'.")
            return _style_reply((False, " ".join(messages)))
        ok, msg = result
        messages.append(msg)
        if not ok:
            return _style_reply((False, " ".join(messages)))
        if i < len(steps):
            slow = re.search(r"\b(chrome|profile|youtube|website|go to|open)\b", step.lower())
            time.sleep(3.0 if slow else 1.0)               # let windows / pages appear before the next step
    return _style_reply((True, " ".join(messages)))


# Open the brain's network connection in the background right away, so the first spoken command isn't slow.
if not os.getenv("VAANI_NO_WARMUP"):
    try:
        import gemini_planner as _gp
        _gp.warm_up()
    except Exception:
        pass