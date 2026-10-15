"""VAANI long-term memory: remembers you between restarts. Stored in memory.db (SQLite) in VAANI's data folder.

Voice commands (handle(text)):
    "remember that my favourite song is Believer"      "my name is Vitthal" / "call me Chief"
    "what do you remember"                             "forget my favourite song"      "forget everything"
    "when I say tmqc I mean Taarak Mehta Ka Ooltah Chashmah"   -> teaches the speech corrector (vocab.json)
    "play my favourite song" / "open my usual browser" -> uses what it remembered
Automatic: counts the apps/sites/songs you use, so context_for() can tell the model your habits.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time

import paths

log = logging.getLogger("vaani.longmem")
_lock = threading.Lock()
_conn = None
_conn_dir = None


def _db():
    global _conn, _conn_dir
    d = paths.data_dir()
    if _conn is None or _conn_dir != d:
        _conn = sqlite3.connect(str(d / "memory.db"), check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)")
        _conn.execute("CREATE TABLE IF NOT EXISTS usage (kind TEXT, name TEXT, n INTEGER NOT NULL, ts REAL NOT NULL, PRIMARY KEY(kind, name))")
        _conn.commit()
        _conn_dir = d
    return _conn


def _clean_key(k: str) -> str:
    k = re.sub(r"\b(?:favorite)\b", "favourite", k.lower())
    k = re.sub(r"^(?:my|the|your)\s+", "", k.strip())
    return re.sub(r"\s+", " ", k).strip(" .,:;!?")[:60]


# ---------------------------------------------------------------- facts
def remember(key: str, value: str) -> None:
    key, value = _clean_key(key), re.sub(r"\s+", " ", value).strip(" .,!?")[:300]
    if not key or not value:
        return
    with _lock:
        c = _db()
        c.execute("INSERT INTO facts(key, value, ts) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                  (key, value, time.time()))
        c.commit()


def get(key: str):
    key = _clean_key(key)
    with _lock:
        row = _db().execute("SELECT value FROM facts WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def forget(key: str) -> int:
    key = _clean_key(key)
    with _lock:
        c = _db()
        n = c.execute("DELETE FROM facts WHERE key=? OR key LIKE ?", (key, f"%{key}%")).rowcount
        c.commit()
    return n


def forget_all() -> None:
    with _lock:
        c = _db()
        c.execute("DELETE FROM facts")
        c.execute("DELETE FROM usage")
        c.commit()


def all_facts() -> list:
    with _lock:
        return _db().execute("SELECT key, value FROM facts ORDER BY ts DESC").fetchall()


def recall(query: str, k: int = 6) -> list:
    """Facts most relevant to the words in `query`, newest first when tied."""
    words = {w for w in re.findall(r"[a-z]{3,}", (query or "").lower())}
    scored = []
    for key, value in all_facts():
        text = f"{key} {value}".lower()
        score = sum(1 for w in words if w in text)
        scored.append((score, key, value))
    scored.sort(key=lambda t: -t[0])
    hits = [(key, value) for score, key, value in scored if score > 0][:k]
    return hits


# ---------------------------------------------------------------- habits
def bump(kind: str, name: str) -> None:
    name = re.sub(r"\s+", " ", name.lower()).strip()[:80]
    if not name:
        return
    with _lock:
        c = _db()
        c.execute("INSERT INTO usage(kind,name,n,ts) VALUES(?,?,1,?) ON CONFLICT(kind,name) DO UPDATE SET n=n+1, ts=excluded.ts",
                  (kind, name, time.time()))
        c.commit()


def top(kind: str, n: int = 3) -> list:
    with _lock:
        return [r[0] for r in _db().execute("SELECT name FROM usage WHERE kind=? ORDER BY n DESC, ts DESC LIMIT ?", (kind, n)).fetchall()]


def context_for(text: str) -> str:
    """A short block for the model: relevant facts + habits. Empty string when nothing is known."""
    lines = [f"{k}: {v}" for k, v in recall(text, 5)]
    name = get("name")
    if name and not any(l.startswith("name:") for l in lines):
        lines.insert(0, f"name: {name}")
    apps, songs = top("app", 3), top("song", 3)
    if apps:
        lines.append("often opens: " + ", ".join(apps))
    if songs:
        lines.append("often plays: " + ", ".join(songs))
    return "; ".join(lines)[:600]


def note_command(text: str, ok: bool) -> None:
    """Learn habits from a command that worked."""
    if not ok:
        return
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?")
    m = re.fullmatch(r"(?:please )?(?:open|launch|start) (.+)", t)
    if m and len(m.group(1)) < 40:
        bump("app", m.group(1))
    m = re.fullmatch(r"(?:please )?play (.+?)(?: on youtube| on spotify)?", t)
    if m and len(m.group(1)) < 60:
        bump("song", m.group(1))


# ------------------------------------------------------------ voice commands
def handle(text: str):
    """Returns (ok, reply) when the sentence is a memory command, else None."""
    t = re.sub(r"\s+", " ", (text or "").strip()).strip(" .!?")
    low = t.lower()
    if not low:
        return None

    m = re.fullmatch(r"(?:when i say|if i say|whenever i say) (.+?) (?:i mean|i meant|it means|means|mean) (.+)", t, re.I)
    if m:
        import vocab
        vocab.add_alias(m.group(1), m.group(2))
        return True, f"Got it, Chief. Whenever you say {m.group(1)}, I'll understand {m.group(2)}."

    if re.fullmatch(r"(?:what do you (?:remember|know) about me|what do you remember|what have you remembered|tell me what you remember)", low):
        facts = all_facts()
        if not facts:
            return True, "I don't have anything saved yet, Chief. Tell me to remember something."
        bits = [f"{k} is {v}" for k, v in facts[:6]]
        return True, "Here's what I remember. " + "; ".join(bits) + "."

    if low in {"forget everything", "forget everything about me", "clear your memory", "erase your memory"}:
        forget_all()
        return True, "Done, Chief. I've forgotten everything."

    m = re.fullmatch(r"forget (?:that |about |my )?(.+)", low)
    if m and m.group(1) not in {"it", "that", "this"}:
        n = forget(m.group(1))
        return (True, f"Okay, I've forgotten {m.group(1)}.") if n else (False, f"I don't have anything saved about {m.group(1)}.")

    m = re.fullmatch(r"(?:please )?remember (?:that )?(?:my |the )?(.+?) (?:is|are|was|=) (.+)", t, re.I)
    if m:
        remember(m.group(1), m.group(2))
        return True, f"Okay Chief, I'll remember that your {_clean_key(m.group(1))} is {m.group(2).strip(' .!?')}."
    m = re.fullmatch(r"(?:please )?remember (?:that )?(.+)", t, re.I)
    if m:
        remember("note " + str(int(time.time()) % 100000), m.group(1))
        return True, "Okay, I'll remember that."

    m = re.fullmatch(r"(?:my name is|i am called|i'm called|call me|you can call me) (.+)", t, re.I)
    if m and len(m.group(1)) < 30:
        remember("name", m.group(1))
        return True, f"Nice to meet you, {m.group(1).strip(' .!?')}. I'll remember that."

    m = re.fullmatch(r"(?:what(?:'s| is) my|do you know my|tell me my) (.+)", low)
    if m:
        v = get(m.group(1))
        if v:
            return True, f"Your {_clean_key(m.group(1))} is {v}."
    return None


def rewrite(text: str):
    """'play my favourite song' -> 'play <the remembered song>'. None when nothing to substitute."""
    low = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?")
    m = re.fullmatch(r"((?:please )?(?:play|put on|open|search for|search|watch)) my ((?:favou?rite )?[a-z ]{3,30})", low)
    if not m:
        return None
    v = get(m.group(2))
    return f"{m.group(1)} {v}" if v else None