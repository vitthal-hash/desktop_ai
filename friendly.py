"""VAANI personality helpers: short natural names, varied replies, and mood awareness.

short_name(title, url)  'vitthal-hash/desktop_ai: ...' + github.com  ->  'GitHub'
pick(key, **fmt)        a varied, human phrase for an action ("Moved to GitHub, Chief.")
detect_mood(text)       'sad' | 'stressed' | 'angry' | 'tired' | 'happy' | 'bored' | None
empathy(mood)           one warm line to put before the answer
"""
from __future__ import annotations

import random
import re
import urllib.parse

BRANDS = {
    "github.com": "GitHub", "youtube.com": "YouTube", "youtu.be": "YouTube", "claude.ai": "Claude",
    "mail.google.com": "Gmail", "gmail.com": "Gmail", "web.whatsapp.com": "WhatsApp", "whatsapp.com": "WhatsApp",
    "chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT", "google.com": "Google", "drive.google.com": "Google Drive",
    "docs.google.com": "Google Docs", "calendar.google.com": "Calendar", "maps.google.com": "Maps",
    "linkedin.com": "LinkedIn", "instagram.com": "Instagram", "facebook.com": "Facebook", "x.com": "X",
    "twitter.com": "X", "reddit.com": "Reddit", "netflix.com": "Netflix", "amazon.in": "Amazon", "amazon.com": "Amazon",
    "flipkart.com": "Flipkart", "spotify.com": "Spotify", "stackoverflow.com": "Stack Overflow",
    "wikipedia.org": "Wikipedia", "discord.com": "Discord", "outlook.live.com": "Outlook", "gemini.google.com": "Gemini",
    "classroom.google.com": "Classroom", "colab.research.google.com": "Colab", "kaggle.com": "Kaggle",
}
_SEP = re.compile(r"\s[-–—|·•:]\s|[:/|]|\s\(\d+\)")


def short_name(title: str = "", url: str = "", limit: int = 28) -> str:
    """A name a person would say out loud for this page/tab."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    for dom, brand in BRANDS.items():
        if host == dom or host.endswith("." + dom):
            return brand
    t = re.sub(r"^\(\d+\)\s*", "", title or "").strip()          # '(3) Inbox' -> 'Inbox'
    t = _SEP.split(t)[0].strip() if t else ""
    if not t and host:
        t = host.split(".")[0].capitalize()
    t = re.sub(r"[^\w\s&'.+#-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
    return t or "that tab"


def short_label(text: str, limit: int = 40) -> str:
    """Shorten a clicked link/button/result text for speech."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = _SEP.split(t)[0].strip() or t
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
    return t


_POOLS = {
    "switch_tab": ["Moved to {name}.", "Here's {name}.", "Switched to {name}.", "{name} is up."],
    "click": ["Opened {name}.", "Clicked {name}.", "Got it, {name}."],
    "click_plain": ["Done.", "Clicked it.", "Got it."],
    "scroll_ok": ["Scrolled {d}.", "Going {d}.", "Here you go."],
    "scroll_fail": ["I tried, but this page won't scroll. Click on it once and say it again?"],
    "new_tab": ["New tab is ready.", "Opened a fresh tab.", "New tab, ready."],
    "close_tab": ["Closed it.", "That tab is gone."],
    "typed": ["Typed it.", "Done, I typed that.", "It's in."],
    "generic": ["Done.", "All set.", "Sorted.", "Done and done."],
}
_last: dict = {}


def pick(key: str, **fmt) -> str:
    pool = _POOLS.get(key) or _POOLS["generic"]
    choices = [p for p in pool if p != _last.get(key)] or pool
    choice = random.choice(choices)
    _last[key] = choice
    try:
        return choice.format(**fmt)
    except Exception:
        return choice


# ------------------------------------------------------------------ mood
_MOODS = [
    ("angry", r"\b(?:annoy\w*|irritat\w*|angry|furious|pissed|hate (?:this|it)|so frustrat\w*|fed up|useless|stupid|not working|doesn'?t work)\b"),
    ("stressed", r"\b(?:stress\w*|anxious|anxiety|overwhelm\w*|worried|nervous|panick?\w*|deadline|exam tomorrow|too much work|under pressure)\b"),
    ("sad", r"\b(?:sad|depress\w*|lonely|upset|cry\w*|heart ?broken|feeling low|feel low|down today|not feeling (?:good|great|well)|miss (?:her|him|home))\b"),
    ("tired", r"\b(?:tired|exhausted|sleepy|drained|burn(?:t|ed) out|no energy|can'?t focus)\b"),
    ("bored", r"\b(?:bored|boring|nothing to do)\b"),
    ("happy", r"\b(?:happy|excited|great news|awesome day|good mood|feeling good|feeling great|got the job|i passed|i won|celebrat\w*)\b"),
]
_EMPATHY = {
    "angry": ["Sorry about that, Chief. Let me sort it out.", "I hear you, Chief. Let's fix this."],
    "stressed": ["Take a breath, Chief. We'll get through it together.", "No stress, Chief. I'm right here."],
    "sad": ["I'm sorry you're feeling low, Chief. I'm here with you.", "Hey Chief, it's okay. I've got you."],
    "tired": ["You sound tired, Chief. Let me take the work off you.", "Rest a little, Chief. I'll handle this."],
    "bored": ["Let's make it fun, Chief.", "Okay Chief, let's liven things up."],
    "happy": ["That's wonderful, Chief!", "Love to hear it, Chief!"],
}


def detect_mood(text: str):
    t = (text or "").lower()
    for mood, pattern in _MOODS:
        if re.search(pattern, t):
            return mood
    return None


def empathy(mood: str) -> str:
    return random.choice(_EMPATHY.get(mood, [""])) if mood else ""


def mood_hint(mood: str) -> str:
    """Extra instruction for the chat model so its tone matches how Chief feels."""
    return {
        "angry": "Chief sounds frustrated. Apologise briefly, stay calm, and fix or explain simply. No lectures.",
        "stressed": "Chief sounds stressed. Be calm and reassuring, offer one small next step.",
        "sad": "Chief sounds sad. Be gentle, warm and kind like a close friend. Do not rush to advice; offer to play something comforting.",
        "tired": "Chief sounds tired. Keep it very short and soft; offer to take work off them.",
        "bored": "Chief is bored. Be playful and suggest one fun thing.",
        "happy": "Chief is happy. Share the joy warmly and briefly.",
    }.get(mood, "")