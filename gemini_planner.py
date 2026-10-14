"""VAANI command brain (Gemini). Turns natural speech into a validated multi-step plan.

plan_steps(user_text, context) -> (steps, ask, error)
  steps : list of {"do": <action>, ...args}   (already validated against the allow-list below)
  ask   : a short clarifying question when Chief's request is truly ambiguous (else "")
  error : text when the brain could not be used (no key, network, bad JSON...). Never raises.

Speed: one cached client, thinking disabled where the model supports it, short prompt, 15 s timeout (Google's minimum is 10 s).
Model: GEMINI_MODEL (default gemini-3.1-flash-lite). Key: GEMINI_API_KEY (from the environment or .env).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

import env_loader  # noqa: F401  (loads GEMINI_API_KEY from .env)

log = logging.getLogger("vaani.brain")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
TIMEOUT_MS = max(10000, int(os.getenv("VAANI_BRAIN_TIMEOUT_MS", "15000")))   # Google refuses deadlines under 10 s
MAX_STEPS = 6

HOTKEYS = ["copy", "paste", "cut", "select_all", "undo", "redo", "save", "find", "print", "minimize", "maximize",
           "switch_window", "show_desktop", "task_manager", "snip", "fullscreen", "lock"]
KEYS = ["enter", "esc", "tab", "space", "backspace", "delete", "up", "down", "left", "right", "home", "end", "pageup", "pagedown"]
MEDIA_OPS = ["play", "pause", "toggle", "mute", "unmute", "next", "previous", "skip_ahead", "skip_back", "faster", "slower", "fullscreen"]

# action -> {arg: (kind, required, extra)}
STR, INT, ENUM, BOOL, URL = "str", "int", "enum", "bool", "url"
SPEC: dict = {
    "new_tab": {"url": (URL, False, None)},
    "close_tab": {}, "back": {}, "forward": {}, "reload": {}, "read_page": {}, "summarize_page": {},
    "goto_tab": {"to": (ENUM, False, ["next", "prev", "first", "last"]), "index": (INT, False, (1, 60))},
    "switch_tab": {"query": (STR, True, None)},
    "open_site": {"query": (STR, False, None), "url": (URL, False, None)},
    "search_web": {"query": (STR, True, None)},
    "search_youtube": {"query": (STR, True, None)},
    "play_youtube": {"query": (STR, True, None)},
    "click_result": {"index": (INT, True, (1, 30))},
    "click_link": {"index": (INT, True, (1, 80))},
    "click": {"query": (STR, True, None)},
    "type": {"text": (STR, True, None), "submit": (BOOL, False, None), "field": (STR, False, None)},
    "scroll": {"direction": (ENUM, False, ["up", "down", "top", "bottom"])},
    "zoom": {"op": (ENUM, True, ["in", "out", "reset"])},
    "media": {"op": (ENUM, True, MEDIA_OPS), "seconds": (INT, False, (1, 600))},
    "open_app": {"name": (STR, True, None)},
    "close_window": {},
    "open_folder": {"name": (STR, True, None)},
    "open_settings": {"name": (STR, True, None)},
    "volume": {"op": (ENUM, True, ["up", "down", "mute", "unmute", "set", "max", "min"]),
               "level": (INT, False, (0, 100)), "amount": (INT, False, (1, 100))},
    "brightness": {"op": (ENUM, True, ["up", "down", "set", "max", "min"]),
                   "level": (INT, False, (0, 100)), "amount": (INT, False, (1, 100))},
    "screenshot": {},
    "hotkey": {"name": (ENUM, True, HOTKEYS)},
    "press": {"key": (ENUM, True, KEYS)},
    "type_text": {"text": (STR, True, None)},
    "media_key": {"op": (ENUM, True, ["play_pause", "next", "previous"])},
    "look": {"question": (STR, False, None)},
    "screen_click": {"target": (STR, True, None), "kind": (ENUM, False, ["click", "double", "right"])},
    "screen_task": {"goal": (STR, True, None)},
    "time": {}, "date": {},
    "calc": {"expression": (STR, True, None)},
    "send_whatsapp": {"contact": (STR, True, None), "message": (STR, True, None)},
    "chat": {},
}

PROMPT = """You are VAANI's command brain on Chief's Windows laptop. Chief talks naturally (speech-to-text, so small mishearings happen). Work out the INTENT using the context, then reply with ONE JSON object:
{"steps":[{"do":"<action>", ...args}], "ask":""}
Use as few steps as possible (max 6). Pure conversation or a knowledge question -> {"steps":[{"do":"chat"}]}. If you genuinely cannot tell what Chief wants -> {"steps":[],"ask":"<one short question>"}. Never invent page contents, results or URLs of sites you are unsure about.
ACTIONS (args):
BROWSER (Chrome, acts on the tab Chief is looking at): new_tab{url?} close_tab goto_tab{to:next|prev|first|last | index:N} switch_tab{query: words from the tab title/site}
 open_site{query: site name or domain | url} search_web{query} search_youtube{query} play_youtube{query: the song/video to play NOW}
 click_result{index: Nth search result/video on the page} click_link{index} click{query: visible text of a button/link/menu/checkbox}
 type{text, submit?:bool, field?: hint of the box} scroll{direction:up|down|top|bottom} back forward reload zoom{op:in|out|reset}
 media{op:play|pause|toggle|mute|unmute|next|previous|skip_ahead|skip_back|faster|slower|fullscreen, seconds?} (video/music playing in the browser)
 read_page summarize_page
COMPUTER: open_app{name} close_window open_folder{name} open_settings{name} volume{op:up|down|mute|unmute|set|max|min, level?:0-100 exact percent, amount?:percent to change by} brightness{op:up|down|set|max|min, level?, amount?} screenshot
 hotkey{name: copy|paste|cut|select_all|undo|redo|save|find|print|minimize|maximize|switch_window|show_desktop|task_manager|snip|fullscreen|lock}
 press{key: enter|esc|tab|space|backspace|delete|up|down|left|right|home|end|pageup|pagedown} type_text{text} (types into the focused app)
 media_key{op:play_pause|next|previous} (system-wide media keys: Spotify, any player)
SCREEN (VAANI looks at a screenshot): look{question} screen_click{target, kind?:click|double|right} screen_task{goal} (multi-step job in any app/popup/installer)
OTHER: time date calc{expression} send_whatsapp{contact,message}
RULES:
- "play / put on <song, artist, video>" -> play_youtube (query = clean search text, e.g. "Believer Imagine Dragons"). "search X" -> search_web, unless Chief says YouTube or context.browser.url is YouTube -> search_youtube.
- "this / that / it / here / the one" refer to what is on screen: use context.window, context.browser, context.page, context.recent.
- Pause/resume/skip/mute of what is playing: media if context.browser.media exists, otherwise media_key (or volume for sound level).
- For web pages prefer browser actions; for native apps, popups, dialogs, installers use screen_click / screen_task. Questions about what is on screen -> look.
- "close this": close_tab if Chief is in Chrome, otherwise close_window.
- "first/second/third link, result, video, website, site" on a page of search results or videos -> click_result with that index (never open_app).
- Understand feelings and complaints as requests: too loud/hurts my ears -> volume down; can't hear -> volume up; too dark/dim -> brightness up;
  too bright/hurts my eyes -> brightness down; can't read it/text too small -> zoom in; boring/not this one/skip -> media next;
  "I'm sad/tired/in a good mood, play something" -> play_youtube with a fitting song search (e.g. "relaxing songs", "happy upbeat songs");
  "where am I / what is this page" -> read_page; "I'm lost / what should I click" -> look.
- open_app is ONLY for real application names (Notepad, Spotify, Calculator...). Never put page words (link, result, first, video, tab) in it.
- Exact levels: "volume to 50 percent" -> volume{op:set,level:50}; "brightness max / full" -> brightness{op:max}; "volume half" -> level 50; "lower brightness by 20" -> {op:down,amount:20}.
- If a request is unclear or could mean two things (which tab? which video? which person?), do NOT guess: ask ONE short, friendly question in "ask".
- "close the current window" / "close this window" -> close_window. Scroll up/down/top/bottom -> scroll.
- Ads are skipped automatically by the browser extension, so never add a "skip ad" step after play_youtube.
EXAMPLES:
"set the volume to fifty percent" -> {"steps":[{"do":"volume","op":"set","level":50}]}
"brightness to maximum" -> {"steps":[{"do":"brightness","op":"max"}]}
"open that one" (nothing clear on screen) -> {"steps":[],"ask":"Which one do you mean, Chief?"}
"it's too loud" -> {"steps":[{"do":"volume","op":"down"}]}
"I can't read this, the text is tiny" -> {"steps":[{"do":"zoom","op":"in"}]}
"I'm feeling sad, play something nice" -> {"steps":[{"do":"play_youtube","query":"soothing feel good songs"}]}
"take me to the second website" (page.results present) -> {"steps":[{"do":"click_result","index":2}]}
"open a new tab and search for best laptops under 50000" -> {"steps":[{"do":"new_tab"},{"do":"search_web","query":"best laptops under 50000"}]}
"play something by Arijit Singh" -> {"steps":[{"do":"play_youtube","query":"Arijit Singh songs"}]}
"make it a bit louder" -> {"steps":[{"do":"volume","op":"up"}]}
"pause" (browser.media present) -> {"steps":[{"do":"media","op":"pause"}]}
"next song" (no browser media) -> {"steps":[{"do":"media_key","op":"next"}]}
"go back" -> {"steps":[{"do":"back"}]}
"open the third one" (page.results present) -> {"steps":[{"do":"click_result","index":3}]}
"click sign in" -> {"steps":[{"do":"click","query":"sign in"}]}
"what is this error on my screen" -> {"steps":[{"do":"look","question":"What is the error shown and what should I do about it?"}]}
"turn on dark mode in windows settings" -> {"steps":[{"do":"screen_task","goal":"Turn on dark mode in Windows Settings"}]}
"switch to the youtube tab" -> {"steps":[{"do":"switch_tab","query":"youtube"}]}
"who invented the telephone" -> {"steps":[{"do":"chat"}]}
CONTEXT (JSON):
"""


# ----------------------------------------------------------------------------- validation
def _coerce(kind: str, extra: Any, value: Any):
    if kind == STR:
        v = str(value).strip()
        return v[:400] if v else None
    if kind == INT:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        lo, hi = extra
        return n if lo <= n <= hi else None
    if kind == ENUM:
        v = str(value).strip().lower().replace(" ", "_")
        return v if v in extra else None
    if kind == BOOL:
        return bool(value) if isinstance(value, bool) else str(value).lower() in {"true", "yes", "1"}
    if kind == URL:
        v = str(value).strip()
        return v if re.match(r"^https?://[^\s]+$", v, re.I) else None
    return None


def validate_steps(raw: Any) -> tuple:
    """-> (steps, error). Unknown actions / bad args are rejected, never executed."""
    if not isinstance(raw, list):
        return [], "Brain returned no step list."
    steps = []
    for item in raw[:MAX_STEPS]:
        if not isinstance(item, dict):
            return [], "Brain returned a malformed step."
        name = str(item.get("do", "")).strip()
        spec = SPEC.get(name)
        if spec is None:
            return [], f"Brain chose an action that isn't allowed: {name!r}."
        clean: dict = {"do": name}
        for arg, (kind, required, extra) in spec.items():
            val = _coerce(kind, extra, item[arg]) if arg in item and item[arg] not in (None, "") else None
            if val is None and required:
                return [], f"Brain's step {name} is missing a valid {arg}."
            if val is not None:
                clean[arg] = val
        if name == "open_site" and not (clean.get("query") or clean.get("url")):
            return [], "Brain's open_site had no target."
        if name == "goto_tab" and not (clean.get("to") or clean.get("index")):
            return [], "Brain's goto_tab had no target."
        steps.append(clean)
    if any(s["do"] == "chat" for s in steps):
        steps = [{"do": "chat"}] if len(steps) == 1 else [s for s in steps if s["do"] != "chat"]
    return steps, None


# ----------------------------------------------------------------------------- Gemini call
_client = None
_client_lock = threading.Lock()
_think_idx = 0
_THINK_OPTIONS = [{"thinking_config": {"thinking_budget": 0}}, {"thinking_config": {"thinking_level": "minimal"}}, {}]


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            from google import genai
            key = os.getenv("GEMINI_API_KEY", "").strip()
            try:
                from google.genai import types
                _client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=TIMEOUT_MS))
            except Exception:
                _client = genai.Client(api_key=key)
        return _client


def _generate(prompt: str) -> str:
    """Call Gemini, negotiating the cheapest 'thinking' setting the model accepts (remembered after the first call).
    A temporary 503 'high demand' is retried once."""
    global _think_idx
    client = _get_client()
    last_exc = None
    retried = False
    while _think_idx < len(_THINK_OPTIONS):
        cfg = {"response_mime_type": "application/json", "temperature": 0, "max_output_tokens": 500, **_THINK_OPTIONS[_think_idx]}
        try:
            resp = client.models.generate_content(model=MODEL, contents=prompt, config=cfg)
            return (resp.text or "").strip()
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if ("503" in msg or "unavailable" in msg or "overloaded" in msg) and not retried:
                retried = True
                time.sleep(0.7)                                   # momentary demand spike: try once more
                continue
            if "thinking" in msg and _think_idx < len(_THINK_OPTIONS) - 1:
                _think_idx += 1                                   # this model doesn't take that thinking option; try the next
                continue
            raise
    raise last_exc or RuntimeError("no Gemini configuration worked")


def available() -> bool:
    if os.getenv("VAANI_NO_BRAIN"):                         # used by the self-tests to force offline behaviour
        return False
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def plan_steps(user_text: str, context: dict | None = None) -> tuple:
    """-> (steps, ask, error)"""
    if not available():
        return [], "", "GEMINI_API_KEY isn't set (put it in the .env file next to main.py)."
    started = time.monotonic()
    try:
        raw = _generate(PROMPT + json.dumps(context or {}, ensure_ascii=False) + '\nChief said: "' + user_text.replace('"', "'") + '"')
    except ImportError:
        return [], "", "The google-genai package isn't installed (pip install google-genai)."
    except Exception as exc:
        log.warning("brain call failed: %s: %s", type(exc).__name__, str(exc)[:200])
        return [], "", f"Gemini request failed ({type(exc).__name__}): {str(exc)[:160]}"
    took = int((time.monotonic() - started) * 1000)
    log.info("brain %dms (%s): %s", took, MODEL, raw[:500])
    if not raw:
        return [], "", "The brain returned an empty answer."
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except Exception:
            data = None
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return [], "", f"The brain's answer wasn't valid JSON: {raw[:100]}"
    ask = str(data.get("ask") or "").strip()[:200]
    steps, err = validate_steps(data.get("steps", []))
    if err:
        log.warning("brain plan rejected: %s | %s", err, raw[:300])
        return [], "", err
    if not steps and not ask:
        return [], "", "The brain didn't produce any action."
    return steps, ask, None


def warm_up() -> None:
    """Open the HTTPS connection in the background so the first real command is fast."""
    def run():
        try:
            if available():
                _generate('Reply with {"steps":[{"do":"chat"}]}')
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="VAANI-BrainWarmup").start()


# ----------------------------------------------------------------------------- backwards compatibility
def plan_command(user_text: str, browser_state: dict | None = None):
    """Old single-action API kept so older tools.py files keep importing cleanly. Returns (None, error)."""
    return None, "plan_command is retired; update tools.py (it now uses plan_steps)."