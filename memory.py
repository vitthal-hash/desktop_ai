"""VAANI short-term memory: understands follow-ups so Chief doesn't have to repeat himself.

    "increase the volume"  ... "more"            -> volume up again
    "scroll down"          ... "a bit more"      -> scroll down again
    "open youtube"         ... "play this song X"-> plays X ON YouTube (no Google search)
    "volume up" ... "that's too much" / "less"   -> volume down
    anything safe          ... "again"           -> repeats the last command

resolve(text)            -> None (nothing to resolve) | ("say", rewritten_command) | ("ask", question)
note(text, ok, message)  -> remember what just happened (call after every command)
"""
from __future__ import annotations

import re
import time
from typing import Optional

_TOPIC_TTL = 180          # seconds a "more / again" still refers to the last volume/scroll/etc.
_SITE_TTL = 900           # seconds "play X" still means "on the site I just opened"

_state = {"topic": None, "dir": None, "mag": "", "t": 0.0, "site": None, "site_t": 0.0, "last_text": "", "last_ok": False}

_UP = r"increase|raise|up|higher|louder|brighter|boost|bump"
_DOWN = r"decrease|lower|down|reduce|quieter|softer|dimmer|dim|drop"
_TOPICS = {
    "volume": r"\b(?:volume|sound|audio)\b|\b(?:louder|quieter|softer)\b",
    "brightness": r"\b(?:brightness|brighter|dimmer|dim)\b",
    "scroll": r"\bscroll(?:ing)?\b|\bpage (?:up|down)\b",
}
_SITE_WORDS = ["youtube", "spotify", "netflix", "github", "google", "gmail", "whatsapp", "chatgpt", "amazon", "flipkart",
               "instagram", "linkedin", "reddit", "wikipedia", "hotstar", "prime video"]
_UNSAFE_REPEAT = re.compile(r"\b(?:type|write|send|message|delete|erase|clear|close|quit|shutdown|restart|lock|format|install|uninstall)\b")
_FOLLOW_CUE = re.compile(
    r"\b(?:more|again|further|keep going|go on|one more|a bit|a little|a lot|less|back|too much|too far|opposite|other way|"
    rf"increase|raise|decrease|lower|reduce|{_UP}|{_DOWN})\b")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip(" .,!?")


def _mag(t: str) -> str:
    if re.search(r"\b(?:a lot|much more|way more|lots)\b", t):
        return " a lot"
    if re.search(r"\b(?:a bit|a little|little|slightly|tiny|touch)\b", t):
        return " a bit"
    return ""


def _topic_of(t: str):
    for name, pat in _TOPICS.items():
        if re.search(pat, t):
            return name
    return None


def _dir_of(t: str):
    up = re.search(rf"\b(?:{_UP})\b", t)
    down = re.search(rf"\b(?:{_DOWN})\b", t)
    if up and (not down or up.start() < down.start()):
        return "up"
    if down:
        return "down"
    return None


def _foreground_site() -> Optional[str]:
    try:
        import pygetwindow
        w = pygetwindow.getActiveWindow()
        title = (w.title or "").lower() if w else ""
    except Exception:
        return None
    for s in _SITE_WORDS:
        if s in title:
            return s
    return None


# ------------------------------------------------------------------ remember
def note(text: str, ok: bool = True, message: str = "") -> None:
    t = _norm(text)
    if not t:
        return
    now = time.monotonic()
    _state["last_text"], _state["last_ok"] = t, bool(ok)
    topic = _topic_of(t)
    if topic and ok:
        d = _dir_of(t)
        if topic == "scroll":
            m = re.search(r"\b(up|down|top|bottom)\b", t)
            d = {"top": "up", "bottom": "down"}.get(m.group(1), m.group(1)) if m else d or "down"
        elif re.search(r"\b(?:max|maximum|full)\b", t):
            d = "up"
        elif re.search(r"\b(?:min|minimum|lowest)\b", t):
            d = "down"
        _state.update(topic=topic, dir=d, mag=_mag(t), t=now)
    elif ok and re.search(r"\b(?:next|previous|prev)\b.*\btab\b|\btab\b.*\b(?:next|previous)\b", t):
        _state.update(topic="tab", dir="previous" if re.search(r"\b(?:previous|prev)\b", t) else "next", mag="", t=now)
    elif ok and re.search(r"\b(?:next|skip)\b.*\b(?:song|track|video)\b", t):
        _state.update(topic="media", dir="next", mag="", t=now)
    # which site is Chief on now?
    if ok:
        for s in _SITE_WORDS:
            if re.search(rf"\b{re.escape(s)}\b", t) and not re.search(r"\b(?:close|quit|exit)\b", t):
                _state.update(site=s, site_t=now)
                break
        else:
            low = (message or "").lower()
            for s in _SITE_WORDS:
                if s in low and re.search(r"\b(?:opened|opening|showing|playing|switched|moved)\b", low):
                    _state.update(site=s, site_t=now)
                    break


# ------------------------------------------------------------------ resolve
def _current_site() -> Optional[str]:
    fg = _foreground_site()
    if fg:
        return fg
    if _state["site"] and time.monotonic() - _state["site_t"] < _SITE_TTL:
        return _state["site"]
    return None


def resolve(text: str):
    """Return ('say', command) / ('ask', question) / None."""
    t = _norm(text)
    if not t:
        return None
    now = time.monotonic()
    words = t.split()

    # 1) "play this song X" / "search X" while on YouTube -> do it ON YouTube, with filler words removed
    site = _current_site()
    if site == "youtube" and not re.search(r"\b(?:google|chrome|bing|on the web|spotify)\b", t):
        m = re.fullmatch(r"(?:please )?(?:can you |could you )?(play|watch|put on|listen to|search|find|look up|look for)"
                         r"(?: for)?(?: me)?(?: the| this| that| a)?(?: song| video| music| track| one| called| named)*[ ,:-]*(.*)", t)
        if m:
            what = re.sub(r"^(?:called|named|by)\s+", "", m.group(2).strip())
            what = re.sub(r"\s+(?:on|in) youtube$", "", what).strip()
            verb = m.group(1)
            if what and what not in {"it", "that", "this", "something", "anything", "one"}:
                if verb in {"play", "watch", "put on", "listen to"}:
                    return "say", f"play {what} on youtube"
                return "say", f"search youtube for {what}"
            if verb in {"play", "watch", "put on", "listen to"} and not what and len(words) <= 5 and re.search(r"\b(?:this|that|a)\b.*\b(?:song|video|music|track)\b", t):
                return "ask", "Sure, Chief. Which song or video should I play?"

    # 2) follow-ups that refer to the last volume / brightness / scroll / tab
    recent = _state["topic"] and (now - _state["t"] < _TOPIC_TTL)
    if recent and len(words) <= 7 and _FOLLOW_CUE.search(t) and not _topic_of(t) \
            and not re.search(r"\b(?:open|play|search|close|type|click|go to|switch)\b", t):
        topic, last_dir = _state["topic"], _state["dir"]
        if topic in {"volume", "brightness", "scroll"}:
            explicit = _dir_of(t)
            m_scroll = re.search(r"\b(up|down|top|bottom)\b", t)
            if topic == "scroll" and m_scroll:
                d = m_scroll.group(1)
            elif explicit:
                d = explicit
            elif re.search(r"\b(?:less|back|too much|too far|opposite|other way)\b", t):
                if last_dir is None:
                    return "ask", f"Should I turn the {topic} up or down, Chief?"
                d = "down" if last_dir == "up" else "up"
            else:                                                   # more / again / further / keep going
                if last_dir is None:
                    return "ask", f"More or less, Chief?"
                d = last_dir
            mag = _mag(t) or _state["mag"]
            if topic == "scroll":
                return "say", f"scroll {d}"
            return "say", f"{topic} {d}{mag}"
        if topic == "tab" and re.search(r"\b(?:more|again|another|next|one more)\b", t):
            return "say", f"{_state['dir']} tab"
        if topic == "media" and re.search(r"\b(?:again|another|next|one more|more)\b", t):
            return "say", "next song"

    # 3) "again" / "do that again" / "repeat that" -> replay the last safe command
    if re.fullmatch(r"(?:please )?(?:do )?(?:it |that |the same )?(?:thing )?again(?: please)?|repeat (?:that|it)|one more time|same again|do the same", t):
        last = _state["last_text"]
        if last and _state["last_ok"] and not _UNSAFE_REPEAT.search(last):
            return "say", last
        if last:
            return "ask", "I'd rather not repeat that one without checking. What exactly should I do, Chief?"
    return None


def reset() -> None:
    _state.update(topic=None, dir=None, mag="", t=0.0, site=None, site_t=0.0, last_text="", last_ok=False)