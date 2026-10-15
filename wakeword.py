"""VAANI wake words: what you say to get its attention, and how forgivingly it is matched.

Default: "Hey Jarvis" or "Hey Vaani" (either works). A short, distinctive name is much easier for speech recognition
to catch than a common word like "computer".

Change it in .env:
    VAANI_WAKE_WORDS=jarvis,vaani          the names that wake it (the first one is what VAANI says out loud)
    VAANI_WAKE_EXTRA=bunny,funny           extra spellings the recogniser keeps producing for your name (need "hey" first)

match(transcript) -> False | True | "command words"   (True = just the wake word; text = a command said with it)
"""
from __future__ import annotations

import difflib
import os
import re

GREETINGS = {"hey", "hay", "hi", "hello", "okay", "ok", "yo", "a", "hate", "he", "hey,"}

# spellings speech recognition commonly produces for each built-in name
_EXACT = {
    "jarvis": {"jarvis", "jarvas", "jervis", "garvis", "jarvice", "jarvus"},
    "vaani": {"vaani", "vani", "vanie", "vaany", "vany", "vaanee", "vahni", "wani", "bani"},
}


def names() -> list:
    raw = os.getenv("VAANI_WAKE_WORDS", "jarvis,vaani")
    out = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return out or ["jarvis"]


def extras() -> set:
    return {n.strip().lower() for n in os.getenv("VAANI_WAKE_EXTRA", "").split(",") if n.strip()}


def display() -> str:
    """What VAANI says when it tells you how to wake it: 'Hey Jarvis'."""
    return "Hey " + names()[0].capitalize()


def _words(text: str) -> list:
    return re.findall(r"[a-z]+", (text or "").lower().replace("'", ""))


def _is_name(word: str):
    """-> 'exact' | 'fuzzy' | None  (exact may be used without a greeting; fuzzy needs 'hey' first)."""
    for n in names():
        if word in _EXACT.get(n, {n}) or word == n:
            return "exact" if len(n) >= 6 else "fuzzy"        # short names ("vaani") are only trusted after "hey"
        if len(word) >= 4 and difflib.SequenceMatcher(None, word, n).ratio() >= 0.8:
            return "fuzzy"
    if word in extras():
        return "fuzzy"
    return None


def match(transcript: str):
    raw = re.findall(r"[a-z']+", (transcript or "").lower())        # keeps "what's" intact for the command text
    words = [w.replace("'", "") for w in raw]
    for i, w in enumerate(words):
        kind = _is_name(w)
        if not kind:
            continue
        greeted = i > 0 and words[i - 1] in GREETINGS
        if kind == "fuzzy" and not greeted:
            continue
        start = i - 1 if greeted else i
        rest = " ".join(raw[i + 1:]) or " ".join(raw[:start])
        return rest if rest else True
    return False


def strip_prefix(text: str) -> str:
    """Remove a leading 'hey jarvis,' / 'jarvis,' from a command."""
    alt = "|".join(re.escape(n) for n in names() + sorted(extras()) + [v for n in names() for v in _EXACT.get(n, ())])
    return re.sub(rf"^\s*(?:(?:hey|hay|hi|hello|okay|ok|yo)[\s,.-]+)?(?:{alt})[\s,.:;!-]*", "", text or "", flags=re.I).strip()