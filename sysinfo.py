"""VAANI system helpers: status ("battery", "how much RAM"), time/date, finding files by voice, clipboard, dictation.

handle(text) -> Result | None
find_files(query)  searches Desktop / Documents / Downloads / OneDrive / Pictures / Music / Videos (a few seconds at most)
format_dictation(text) -> text with spoken punctuation ("comma", "full stop", "new line") turned into real punctuation
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

Result = tuple
_last_found: dict = {"t": 0.0, "paths": []}
_ORD = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}


# ------------------------------------------------------------------- status
def _status(what: str) -> Result:
    try:
        import psutil
    except ImportError:
        return False, "System status needs psutil installed. Run: pip install psutil"
    if what == "battery":
        b = psutil.sensors_battery()
        if b is None:
            return True, "This PC doesn't report a battery, so it's probably plugged in all the time."
        state = "charging" if b.power_plugged else "on battery"
        left = ""
        if not b.power_plugged and b.secsleft and b.secsleft > 0:
            h, m = divmod(b.secsleft // 60, 60)
            left = f", about {h} hour{'s' if h != 1 else ''} {m} minutes left" if h else f", about {m} minutes left"
        return True, f"Battery is at {int(b.percent)} percent, {state}{left}."
    if what == "cpu":
        return True, f"CPU is at {int(psutil.cpu_percent(interval=0.6))} percent."
    if what == "ram":
        v = psutil.virtual_memory()
        return True, f"You're using {int(v.percent)} percent of your memory, {v.used / 2**30:.1f} of {v.total / 2**30:.0f} gigabytes."
    if what == "disk":
        d = psutil.disk_usage(os.path.abspath(os.sep))
        return True, f"Your main drive is {int(d.percent)} percent full, with {d.free / 2**30:.0f} gigabytes free."
    return False, "I don't know that one."


# -------------------------------------------------------------------- files
def _roots() -> list:
    home = Path.home()
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive", "OneDrive/Documents", "OneDrive/Desktop"]
    return [home / n for n in names if (home / n).is_dir()]


def find_files(query: str, limit: int = 5, budget: float = 4.0) -> list:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in {"file", "files", "document", "folder", "the", "my", "a", "named", "called"}]
    if not words:
        return []
    started, hits = time.monotonic(), []
    skip = {"node_modules", ".git", "__pycache__", "appdata", "site-packages", "$recycle.bin"}
    for root in _roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
            if dirpath[len(str(root)):].count(os.sep) > 5:
                dirnames[:] = []
            for name in filenames + dirnames:
                low = name.lower()
                if all(w in low for w in words):
                    p = os.path.join(dirpath, name)
                    try:
                        hits.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
            if time.monotonic() - started > budget:
                break
    hits.sort(reverse=True)
    return [p for _, p in hits[:limit]]


def _open_path(p: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(p)                                              # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", p])


def _reveal(p: str) -> None:
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])


def _speak_files(paths_: list, opened: bool) -> str:
    names = [Path(p).name for p in paths_]
    if opened and names:
        return f"Opened {names[0]}." if len(names) == 1 else f"Opened {names[0]}. I also found {len(names) - 1} more like it."
    if len(names) == 1:
        return f"Found {names[0]} in {Path(paths_[0]).parent.name}."
    return "I found " + str(len(names)) + ": " + "; ".join(f"{i + 1}. {n}" for i, n in enumerate(names[:3])) + ". Say open the first one, or second one."


# ---------------------------------------------------------------- clipboard
def read_clipboard() -> str:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        except tk.TclError:
            return ""
        finally:
            root.destroy()
    except Exception:
        return ""


# --------------------------------------------------------------- dictation
_PUNCT = [(r"\bnew paragraph\b", "\n\n"), (r"\bnew line\b", "\n"), (r"\bfull stop\b|\bperiod\b", "."), (r"\bcomma\b", ","),
          (r"\bquestion mark\b", "?"), (r"\bexclamation (?:mark|point)\b", "!"), (r"\bcolon\b", ":"), (r"\bsemicolon\b", ";"),
          (r"\bopen bracket\b", "("), (r"\bclose bracket\b", ")"), (r"\bhyphen\b|\bdash\b", "-")]
_FILLER = re.compile(r"\b(?:um+|uh+|erm|hmm+)\b[,]?\s*", re.I)


def format_dictation(text: str) -> str:
    t = _FILLER.sub("", text or "")
    for pat, rep in _PUNCT:
        t = re.sub(r"[ \t]*(?:" + pat + r")[ \t]*", rep + (" " if rep not in {"\n", "\n\n", "(", "-"} else ""), t, flags=re.I)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"[ \t]+\n", "\n", t).strip(" ")
    out, cap = [], True
    for ch in t:                                                      # capitalise the start of each sentence
        out.append(ch.upper() if cap and ch.isalpha() else ch)
        if ch.isalpha():
            cap = False
        elif ch in ".!?\n":
            cap = True
    return "".join(out)


# ---------------------------------------------------------------- router
def handle(text: str):
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?")
    t = re.sub(r"^(?:please |can you |could you |hey )", "", t)

    if re.fullmatch(r"(?:what(?:'s| is) )?(?:my |the )?battery(?: level| percentage| status| left)?|how much battery(?: do i have| is left| left)?|is (?:my )?(?:pc|laptop) charging", t):
        return _status("battery")
    if re.fullmatch(r"(?:what(?:'s| is) )?(?:my |the )?cpu(?: usage| load)?|how (?:busy|loaded) is (?:my )?(?:cpu|pc|computer)", t):
        return _status("cpu")
    if re.fullmatch(r"(?:what(?:'s| is) )?(?:my |the )?(?:ram|memory)(?: usage)?|how much (?:ram|memory) (?:am i using|is used|do i have)", t):
        return _status("ram")
    if re.fullmatch(r"(?:what(?:'s| is) )?(?:my |the )?(?:disk|storage|drive)(?: space| usage)?|how much (?:disk|storage|space) (?:is left|do i have|is free)", t):
        return _status("disk")
    if re.fullmatch(r"(?:what(?:'s| is) )?(?:the )?(?:current )?time(?: is it)?(?: now)?|what time is it(?: now)?|tell me the time", t):
        return True, dt.datetime.now().strftime("It's %I:%M %p.").replace("It's 0", "It's ")
    if re.fullmatch(r"(?:what(?:'s| is) )?(?:the |today's )?(?:date|day)(?: today| is it)?|what day is it(?: today)?|what(?:'s| is) today's date", t):
        return True, dt.datetime.now().strftime("It's %A, %B %d.").replace(" 0", " ")

    if re.fullmatch(r"(?:read|what(?:'s| is) (?:on|in)|tell me what(?:'s| is) (?:on|in)) (?:my |the )?clipboard|read (?:what i )?copied|what did i copy", t):
        c = read_clipboard().strip()
        if not c:
            return True, "Your clipboard is empty."
        return True, "Your clipboard says: " + (c[:280] + ("..." if len(c) > 280 else ""))
    if t in {"clear clipboard", "clear my clipboard", "empty the clipboard"}:
        try:
            import tkinter as tk
            r = tk.Tk(); r.withdraw(); r.clipboard_clear(); r.update(); r.destroy()
            return True, "Clipboard cleared."
        except Exception as exc:
            return False, f"I couldn't clear the clipboard: {exc}"

    # open the Nth file from the last search
    m = re.fullmatch(r"(?:open|show|select|pick)?(?: the)? (first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)(?: one| file| result)?", t)
    if m and _last_found["paths"] and time.monotonic() - _last_found["t"] < 90:
        i = _ORD[m.group(1)] - 1
        if i < len(_last_found["paths"]):
            _open_path(_last_found["paths"][i])
            return True, f"Opened {Path(_last_found['paths'][i]).name}."

    kinds = r"(?:file|files|document|documents|folder|pdf|photo|photos|picture|pictures|presentation|spreadsheet|resume|invoice|screenshot)"
    m = re.fullmatch(r"(?:find|search for|look for|locate|where is|where's)( my| the| a)?( file| document| folder)? ?(?:named |called )?(.+?)(?: on (?:my )?(?:pc|computer|laptop|drive|system))?", t)
    m_open = re.fullmatch(r"(open|show me) my (.+?)(?: file| document| folder)", t)
    if m or m_open:
        if m_open:
            opening, query, personal, kind_word, on_pc = True, m_open.group(2), True, True, False
        else:
            opening = False
            query = m.group(3)
            personal = bool(m.group(1) and m.group(1).strip() == "my")
            kind_word = bool(m.group(2)) or bool(re.search(rf"\b{kinds}\b", query))
            on_pc = bool(re.search(r"on (?:my )?(?:pc|computer|laptop|drive|system)$", t))
        query = re.sub(rf"\b{kinds}\b", "", query).strip() or query
        # a plain "search for python tutorials" is a WEB search, not a file search: need a clear file signal
        if not (kind_word or on_pc or (personal and not re.search(r"\b(?:on|in|at)\b", query))):
            return None
        if len(query) < 2 or query in {"it", "that", "this", "something"} or re.search(r"\b(?:on youtube|on google|online|on the web|news|weather)\b", query):
            return None
        found = find_files(query)
        if not found:
            return False, f"I couldn't find anything called {query} in your Desktop, Documents, Downloads or Pictures."
        _last_found.update(t=time.monotonic(), paths=found)
        if opening:
            _open_path(found[0])
        return True, _speak_files(found, opening)
    return None