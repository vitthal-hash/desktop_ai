"""VAANI vocabulary: fixes names speech-to-text keeps getting wrong ("tmqc" -> Taarak Mehta Ka Ooltah Chashmah).

Edit vocab.json (next to main.py) any time; it reloads automatically. Two lists:
  aliases : what it mishears -> what you mean (exact, spaced-out letters, and close spellings are all caught)
  words   : names you want Whisper to expect (shows, songs, people); they are fed to the recogniser as hints
apply(text) -> corrected text      hint_words() -> list of hint words for the recogniser
"""
from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

_PATH = Path(os.getenv("VAANI_VOCAB", Path(__file__).with_name("vocab.json")))
_cache = {"mtime": None, "aliases": {}, "words": []}


def _load() -> None:
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return
    if mtime == _cache["mtime"]:
        return
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    aliases = {re.sub(r"\s+", " ", str(k).lower()).strip(): str(v) for k, v in (data.get("aliases") or {}).items() if not str(k).startswith("_")}
    _cache.update(mtime=mtime, aliases=aliases, words=[str(w) for w in (data.get("words") or [])])


def hint_words() -> list:
    _load()
    return list(dict.fromkeys(_cache["words"] + list(dict.fromkeys(_cache["aliases"].values()))))


def apply(text: str) -> str:
    """Fix mis-heard names. Safe to run twice: a name that is already correct is left alone."""
    _load()
    aliases = _cache["aliases"]
    if not text or not aliases:
        return text
    out = text
    # protect names that are ALREADY correct, so "taarak mehta" -> full title never doubles up on text that has it
    canon = sorted(set(aliases.values()), key=len, reverse=True)
    for i, c in enumerate(canon):
        out = re.sub(re.escape(c), f"\u2063P{i}\u2063", out, flags=re.I)
    # 1) phrases / spaced-out letters, longest first ("t m k o c", "tm koc")
    for key in sorted(aliases, key=len, reverse=True):
        loose = r"[\s.\-]*".join(re.escape(ch) for ch in key.replace(" ", "")) if len(key.replace(" ", "")) <= 6 else re.escape(key)
        out = re.sub(rf"(?<![A-Za-z0-9]){loose}(?![A-Za-z0-9])", lambda m, k=key: aliases[k], out, flags=re.I)
    # 2) single close misspellings of an alias ("tmqk", "tmcok"): token must look like an acronym/short unknown word
    singles = [k for k in aliases if " " not in k and len(k) >= 3]
    for tok in set(re.findall(r"[A-Za-z0-9']+", out)):
        low = tok.lower()
        if len(low) < 3 or len(low) > 8 or low in aliases:
            continue
        match = difflib.get_close_matches(low, singles, n=1, cutoff=0.75)
        if match and (low.isalpha() and (not re.search(r"[aeiou]", low) or len(low) <= 5)):   # acronym-like, not a normal word
            out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", lambda m, k=match[0]: aliases[k], out)
    for i, c in enumerate(canon):                                  # put the protected names back
        out = out.replace(f"\u2063P{i}\u2063", c)
    return out


def add_alias(heard: str, meant: str) -> None:
    """Teach VAANI: whenever speech-to-text produces `heard`, it means `meant`. Saved into vocab.json."""
    heard = re.sub(r"\s+", " ", str(heard).lower()).strip(" .,!?")
    meant = re.sub(r"\s+", " ", str(meant)).strip(" .,!?")
    if not heard or not meant:
        return
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8")) if _PATH.exists() else {}
    except Exception:
        data = {}
    data.setdefault("aliases", {})[heard] = meant
    words = data.setdefault("words", [])
    if meant not in words:
        words.append(meant)
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache["mtime"] = None                      # force reload on next use