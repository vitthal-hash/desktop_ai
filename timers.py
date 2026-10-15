"""VAANI timers & reminders (saved to timers.json, so they survive a restart).

"set a timer for 5 minutes"  "timer for ten seconds"  "remind me in half an hour to drink water"
"remind me at 5 pm to call mom"  "what timers do I have"  "cancel all timers"
start(notify) launches the background clock; notify(text) is called (and spoken) when something is due.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time

import levels
import paths

log = logging.getLogger("vaani.timers")
_items: list = []                    # [{"id", "due", "text", "kind"}]
_lock = threading.Lock()
_notify = None
_thread = None
_next_id = 1
_UNIT = {"h": 3600, "hr": 3600, "hour": 3600, "m": 60, "min": 60, "minute": 60, "s": 1, "sec": 1, "second": 1}


def _file():
    return paths.data_dir() / "timers.json"


def _save() -> None:
    try:
        _file().write_text(json.dumps(_items), encoding="utf-8")
    except OSError:
        pass


def _load() -> None:
    global _items, _next_id
    try:
        _items = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        _items = []
    _next_id = max([i["id"] for i in _items] + [0]) + 1


def parse_duration(text: str):
    """'five minutes', '1 hour 30 minutes', 'half an hour', 'an hour', '90 seconds' -> seconds, or None."""
    t = levels.words_to_number(text.lower())
    t = re.sub(r"\bhalf an? hour\b", "30 minutes", t)
    t = re.sub(r"\ban? (?=hours?|minutes?|seconds?)", "1 ", t)
    t = re.sub(r"\ba couple of (?=hours?|minutes?)", "2 ", t)
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", t):
        total += float(num) * _UNIT.get(re.sub(r"s$", "", unit), 0)
    return int(total) if total > 0 else None


def parse_clock(text: str):
    """'at 5 pm', 'at 17:30', 'at 7' -> seconds from now to the next time it is that hour, or None."""
    m = re.search(r"\bat (\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", levels.words_to_number(text.lower()))
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    now = dt.datetime.now()
    candidates = []
    if ap or h > 12 or h == 0:                                   # unambiguous hour
        candidates = [h]
    else:                                                        # "at 5": the next 5 o'clock, morning or evening
        candidates = [h, (h + 12) % 24]
    options = []
    for hour in candidates:
        due = now.replace(hour=hour, minute=mi, second=0, microsecond=0)
        if due <= now:
            due += dt.timedelta(days=1)
        options.append(due)
    due = min(options)
    return int((due - now).total_seconds())


def _say_duration(sec: int) -> str:
    if sec >= 3600:
        h, m = divmod(sec // 60, 60)
        return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minute{'s' if m != 1 else ''}" if m else "")
    if sec >= 60:
        m = sec // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{sec} second{'s' if sec != 1 else ''}"


def add(seconds: int, text: str, kind: str) -> dict:
    global _next_id
    with _lock:
        item = {"id": _next_id, "due": time.time() + seconds, "text": text, "kind": kind}
        _next_id += 1
        _items.append(item)
        _save()
    return item


def _ticker() -> None:
    while True:
        time.sleep(1.0)
        due = []
        with _lock:
            now = time.time()
            for it in list(_items):
                if it["due"] <= now:
                    due.append(it)
                    _items.remove(it)
            if due:
                _save()
        for it in due:
            late = time.time() - it["due"]
            msg = ("Chief, your timer is up." if it["kind"] == "timer" else f"Chief, reminder: {it['text']}.")
            if late > 120:
                msg = "Sorry, I was off when this was due. " + msg
            log.info("timer fired: %s", msg)
            try:
                if _notify:
                    _notify(msg)
            except Exception:
                log.exception("timer notify failed")


def start(notify) -> None:
    global _notify, _thread
    _notify = notify
    _load()
    if _thread is None:
        _thread = threading.Thread(target=_ticker, daemon=True, name="VAANI-Timers")
        _thread.start()


def handle(text: str):
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?")
    t = re.sub(r"^(?:please |can you |could you |hey )", "", t)

    m = re.fullmatch(r"(?:set|start|make|create)(?: me)?(?: a| an)?(?: new)? (?:(.+?) )?timer(?: for| of)? (.+?)(?: for (.+))?", t) or \
        re.fullmatch(r"(?:set|start)(?: a| an)? (.+?) timer", t) or re.fullmatch(r"timer (?:for )?(.+)", t)
    if m and "timers" not in t:
        sec = parse_duration(t)
        if sec:
            add(sec, "timer", "timer")
            return True, f"Timer set for {_say_duration(sec)}."
        return False, "For how long should I set the timer, Chief?"

    m = re.fullmatch(r"(?:remind me|set a reminder|reminder)(?: to (.+?))? (in|at) (.+)", t)
    if m:
        what, kind, tail = m.group(1) or "", m.group(2), m.group(3)
        if not what and " to " in tail:
            tail, what = tail.split(" to ", 1)
        when = f"{kind} {tail}"
        sec = parse_duration(when) if kind == "in" else parse_clock(when)
        if not sec:
            return False, "When should I remind you, Chief?"
        if not what.strip():
            return False, "What should I remind you about?"
        what = re.sub(r"\b(?:my|me)\b", lambda mm: {"my": "your", "me": "you"}[mm.group(0)], what.strip())
        add(sec, what, "reminder")
        return True, (f"Okay, I'll remind you to {what} in {_say_duration(sec)}." if kind == "in"
                      else f"Okay, I'll remind you to {what} at {tail}.")

    if re.fullmatch(r"(?:what|which|list|show)(?: are)?(?: my| the| all)? (?:timers?|reminders?)(?: do i have| are set| are running)?|how much time (?:is )?left|(?:any|my) timers?", t):
        with _lock:
            items = sorted(_items, key=lambda i: i["due"])
        if not items:
            return True, "You have no timers or reminders running."
        bits = []
        for it in items[:4]:
            left = max(0, int(it["due"] - time.time()))
            bits.append(f"a timer with {_say_duration(left)} left" if it["kind"] == "timer" else f"a reminder to {it['text']} in {_say_duration(left)}")
        return True, "You have " + ", and ".join(bits) + "."

    if re.fullmatch(r"(?:cancel|stop|delete|clear|remove)(?: all| my| the)?(?: timers?| reminders?| timers? and reminders?)(?: all)?", t):
        with _lock:
            n = len(_items)
            _items.clear()
            _save()
        return True, "Cancelled." if n else "There was nothing to cancel."
    return None