"""VAANI routines: one phrase runs a whole set-up.  "start study mode" -> opens your apps/tabs in order.

"make a routine called study mode that opens github and youtube and notepad"
"start study mode" / "run my morning routine" / "study mode"      "list my routines"     "delete the study mode routine"
Stored in routines.json (data folder); you can also edit it by hand: {"study mode": ["open github", "play lofi on youtube"]}
handle(text, run) -> Result | None      run(command) -> Result is how a single command is executed (with its own verification)
"""
from __future__ import annotations

import difflib
import json
import re
import time

import paths

Result = tuple
_VERBS = r"(?:open|play|search|set|turn|go|start|launch|close|mute|unmute|increase|decrease|lower|raise|switch|snap|maximi[sz]e|minimi[sz]e|scroll|type|take|show|move|run)"


def _file():
    return paths.data_dir() / "routines.json"


def load() -> dict:
    try:
        return {k.lower(): list(v) for k, v in json.loads(_file().read_text(encoding="utf-8")).items() if isinstance(v, list)}
    except Exception:
        return {}


def _save(data: dict) -> None:
    _file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_BASE = ["open", "play", "search", "set", "turn", "go", "start", "launch", "close", "mute", "unmute", "increase", "decrease", "lower",
         "raise", "switch", "snap", "maximize", "minimize", "scroll", "type", "take", "show", "move", "run", "dim", "brighten"]


def _to_command(step: str) -> str:
    s = re.sub(r"\s+", " ", step.strip(" .,")).lower()
    s = re.sub(r"^(?:then |also |and )+", "", s)
    first, _, rest = s.partition(" ")
    for base in _BASE:                                        # "opens" -> "open", "dims" -> "dim", "goes" -> "go"
        if first in {base, base + "s", base + "es"}:
            first = base
            break
    s = (first + " " + rest).strip()
    if re.fullmatch(r"dim(?: the)?(?: screen| display| brightness)?(?: a bit| a little)?", s):
        return "set brightness to 30 percent"
    if re.fullmatch(r"brighten(?: the)?(?: screen| display)?", s):
        return "set brightness to 80 percent"
    return s if first in _BASE else f"open {s}"


def _find(name: str, data: dict):
    name = re.sub(r"\b(?:my|the|a)\b", "", name.lower()).strip()
    name = re.sub(r"\s+(?:routine|mode|workspace)$", "", name).strip()
    if not name:
        return None
    for key in data:
        k = re.sub(r"\s+(?:routine|mode|workspace)$", "", key)
        if k == name or key == name:
            return key
    close = difflib.get_close_matches(name, [re.sub(r"\s+(?:routine|mode|workspace)$", "", k) for k in data], n=1, cutoff=0.82)
    if close:
        return next(k for k in data if re.sub(r"\s+(?:routine|mode|workspace)$", "", k) == close[0])
    return None


def handle(text: str, run, gap: float = 1.4):
    t = re.sub(r"\s+", " ", (text or "").strip()).strip(" .!?")
    low = t.lower()
    data = load()

    m = re.fullmatch(r"(?:please )?(?:make|create|save|add|set up)(?: me)?(?: a| an| new)*(?: new)? (?:routine|workspace|mode)(?: called| named| for)? (.+?)(?: (?:that|which|to|where you|and) (?:you )?(?:will )?(.+))", t, re.I)
    if m:
        name = re.sub(r"^(?:the|my|a)\s+", "", m.group(1).strip().lower()).strip()
        body = m.group(2)
        parts = [p for p in re.split(r",| and then | then | and ", body) if p.strip()]
        if not name or not parts:
            return False, "What should the routine do, Chief?"
        cmds = [_to_command(p) for p in parts]
        data[name] = cmds
        _save(data)
        return True, f"Saved {name}. It will " + ", then ".join(cmds[:5]) + "."

    if re.fullmatch(r"(?:list|show|what are)(?: all)?(?: my)? (?:routines|workspaces|modes)(?: do i have)?", low):
        if not data:
            return True, "You don't have any routines yet. Say, make a routine called study mode that opens GitHub and YouTube."
        return True, "Your routines are " + ", ".join(sorted(data)) + "."

    m = re.fullmatch(r"(?:delete|remove|forget)(?: the| my)? (.+?)(?: routine| mode| workspace)?", low)
    if m and _find(m.group(1), data):
        key = _find(m.group(1), data)
        data.pop(key)
        _save(data)
        return True, f"Deleted {key}."

    m = re.fullmatch(r"what(?:'s| is) in(?: my| the)? (.+?)(?: routine| mode| workspace)?", low)
    if m and _find(m.group(1), data):
        key = _find(m.group(1), data)
        return True, f"{key} will " + ", then ".join(data[key]) + "."

    m = re.fullmatch(r"(?:please )?(?:(?:start|run|begin|activate|launch|do|enable|switch to|go to|turn on)(?: my| the)? )?(.+?)(?: routine| mode| workspace)?", low)
    if m:
        key = _find(m.group(1), data)
        explicit = bool(re.match(r"(?:please )?(?:start|run|begin|activate|launch|do|enable|switch to|go to|turn on)\b", low)) or \
            bool(re.search(r"\b(?:routine|mode|workspace)$", low))
        if key and explicit:
            ok_n, failed = 0, []
            for cmd in data[key]:
                try:
                    import interrupt
                    if interrupt.cancelled():
                        return False, f"Stopped {key} after {ok_n} steps."
                except Exception:
                    pass
                res = run(cmd)
                if res and res[0]:
                    ok_n += 1
                else:
                    failed.append(cmd)
                time.sleep(gap)
            if failed:
                return False, f"{key} is mostly ready, but I couldn't do: {', '.join(failed[:2])}."
            return True, f"{key.capitalize()} is ready, Chief."
    return None