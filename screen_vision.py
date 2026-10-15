"""VAANI screen vision: lets VAANI SEE the screen (native windows too, e.g. Chrome's profile picker).

Uses a screenshot + Gemini vision. Needs GEMINI_API_KEY and `pip install google-genai pyautogui pillow`.
Privacy: a screenshot is sent to Google's Gemini API only when you ask for something visual
(e.g. "what's on my screen", "select the third profile", or a click VAANI can't do through the browser).
Models: GEMINI_VISION_MODEL (optional) -> your GEMINI_MODEL (default gemini-3.1-flash-lite) -> gemini-3.5-flash.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time

import env_loader  # noqa: F401  (loads GEMINI_API_KEY from .env)

log = logging.getLogger("vaani.vision")


def _models() -> list[str]:
    """Vision models to try, in order: GEMINI_VISION_MODEL (if set), your GEMINI_MODEL, then gemini-3.5-flash."""
    names = [os.getenv("GEMINI_VISION_MODEL", "").strip(),
             os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip(), "gemini-3.5-flash"]
    return list(dict.fromkeys(n for n in names if n))


def _capture(max_side: int = 1600):
    """Returns (png_bytes, (full_width, full_height)) in the same pixel space pyautogui clicks in."""
    import pyautogui
    img = pyautogui.screenshot().convert("RGB")
    full = img.size
    if max(full) > max_side:
        s = max_side / max(full)
        img = img.resize((int(full[0] * s), int(full[1] * s)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), full


def _ask(prompt: str, png: bytes, want_json: bool = True):
    """Returns (data, error). Exactly one is None. Uses Claude, Gemini or a local vision model, whichever is set up."""
    import llm
    if not llm.can_see():
        return None, ("I need a model that can see images for that. Add ANTHROPIC_API_KEY or GEMINI_API_KEY to .env, "
                      "or set VAANI_OLLAMA_VISION=qwen2.5vl:7b.")
    return llm.ask("You are VAANI's eyes. Look at the screenshot and answer exactly what was asked.", prompt, png,
                   tier="smart", want_json=want_json)


def _center(box, full):
    """box_2d is [ymin, xmin, ymax, xmax] normalised to 0-1000."""
    ymin, xmin, ymax, xmax = [float(v) for v in box]
    return int((xmin + xmax) / 2000 * full[0]), int((ymin + ymax) / 2000 * full[1])


def _describe_from_controls() -> tuple[bool, str]:
    """No vision model available: describe the active window from its real controls (UI Automation)."""
    import screen_state as ss
    fg = ss.foreground()
    els = ss.elements(40)
    if not fg.get("title") and not els:
        return False, "I can't read the screen without a vision model or UI Automation."
    names = [e["name"] for e in els if e["name"]][:10]
    text = f"You're in {fg.get('title') or 'an app'}."
    if names:
        text += " I can see: " + ", ".join(names[:8]) + "."
    return True, text


def describe(question: str = "") -> tuple[bool, str]:
    import llm
    if not llm.can_see():
        return _describe_from_controls()
    try:
        png, _ = _capture()
    except Exception as exc:
        return False, f"I couldn't capture the screen: {exc}"
    prompt = ("You are VAANI's eyes. The image is the user's current screen. "
              + (f"The user asks: {question}. " if question else "Say what the user is looking at. ")
              + "Answer in at most three short sentences suitable for being read aloud. "
                "Only state what is actually visible; if unsure, say so.")
    text, err = _ask(prompt, png, want_json=False)
    return (False, err) if err else (True, text)


def click_described(description: str, double: bool = False, right: bool = False) -> tuple[bool, str]:
    """Find a UI element by description on screen and click it."""
    try:
        png, full = _capture()
    except Exception as exc:
        return False, f"I couldn't capture the screen: {exc}"
    prompt = (f'Find this on the screen: "{description}". Return JSON: '
              '{"found": true/false, "label": "what you found", "box_2d": [ymin, xmin, ymax, xmax]} '
              "with coordinates normalised to 0-1000. If it is not clearly visible set found=false.")
    data, err = _ask(prompt, png)
    if err:
        return False, err
    if not isinstance(data, dict) or not data.get("found") or not data.get("box_2d"):
        return False, f"I can't see '{description}' on the screen."
    import pyautogui
    x, y = _center(data["box_2d"], full)
    log.info("vision click %r at (%s,%s) label=%r", description, x, y, data.get("label"))
    if right:
        pyautogui.rightClick(x, y)
    else:
        pyautogui.click(x, y, clicks=2 if double else 1)
    return True, f"Clicked {str(data.get('label') or description)[:80]}."


_PROFILE_PROMPT = (
    "This is a Windows screenshot. Is Google Chrome's profile picker ('Who's using Chrome?' / 'Choose a profile') visible? "
    "Return JSON: {\"is_profile_picker\": true/false, \"profiles\": [{\"name\": \"...\", \"box_2d\": [ymin, xmin, ymax, xmax]}]} "
    "with box coordinates normalised to 0-1000. List ONLY the person/profile cards (not Guest, not 'Add', not 'Create' tiles).")


def choose_profile(n: int | None = None, name: str | None = None, focus=None, open_picker=None) -> tuple[bool, str]:
    """Click the n-th (1-based, reading order) or the named profile in Chrome's picker."""
    import pyautogui

    def look():
        png, full = _capture()
        data, err = _ask(_PROFILE_PROMPT, png)
        return data, err, full

    try:
        if focus:
            focus()
            time.sleep(0.4)
        data, err, full = look()
        if err:
            return False, err
        if not (isinstance(data, dict) and data.get("is_profile_picker")):
            if open_picker is None:
                return False, "I don't see Chrome's profile picker on the screen."
            open_picker()
            time.sleep(2.5)
            data, err, full = look()
            if err:
                return False, err
        if not (isinstance(data, dict) and data.get("is_profile_picker") and data.get("profiles")):
            return False, "I couldn't find Chrome's profile picker on the screen."
        profiles = [p for p in data["profiles"] if isinstance(p, dict) and p.get("box_2d") and len(p["box_2d"]) == 4]
        profiles.sort(key=lambda p: (round(float(p["box_2d"][0]) / 80), float(p["box_2d"][1])))   # rows, then left->right
        if name:
            hit = [p for p in profiles if name.lower() in str(p.get("name", "")).lower()]
            if not hit:
                return False, f"I don't see a profile named {name}. I can see: " + ", ".join(str(p.get("name")) for p in profiles) + "."
            target = hit[0]
        else:
            if not n or n < 1 or n > len(profiles):
                return False, f"I can see {len(profiles)} profiles, so there's no number {n}."
            target = profiles[n - 1]
        x, y = _center(target["box_2d"], full)
        log.info("vision profile click %s at (%s,%s)", target.get("name"), x, y)
        pyautogui.click(x, y)
        time.sleep(2.5)
        data2, _, _ = look()
        if isinstance(data2, dict) and data2.get("is_profile_picker"):
            return False, f"I clicked {target.get('name')}, but the profile picker is still showing."
        return True, f"Opened the {target.get('name') or 'selected'} profile."
    except Exception as exc:
        log.exception("choose_profile failed")
        return False, f"Choosing the profile failed: {exc}"


# ======================================================================================
#  SCREEN TASK AGENT: "do this on the screen" in ANY app (look -> act -> look ... up to N steps)
# ======================================================================================
RISKY = re.compile(r"\b(delete|remove|erase|format|uninstall|pay|purchase|buy|order|checkout|transfer|send|submit|post|publish|"
                   r"shut ?down|restart|sign ?out|log ?out|reset|wipe|empty)\b", re.I)
SAFE_KEYS = {"enter", "esc", "tab", "space", "backspace", "delete", "up", "down", "left", "right", "home", "end", "pageup",
             "pagedown", "f5", "f11", "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+f", "ctrl+s",
             "ctrl+t", "ctrl+w", "ctrl+l", "alt+left", "alt+right", "alt+tab", "win+d"}
_TASK_PROMPT = (
    "You operate a Windows PC for the user by looking at screenshots. GOAL: {goal}\n"
    "Steps already done: {history}\n"
    "Look at the current screenshot and decide the SINGLE next action. Return JSON only:\n"
    '{{"action": "click|double_click|right_click|type|press|scroll|wait|done|fail", '
    '"box_2d": [ymin, xmin, ymax, xmax], "text": "", "keys": "", "direction": "up|down", "reason": "", "summary": ""}}\n'
    "- box_2d (0-1000 normalised) is required for click/double_click/right_click: the centre of the exact control to click.\n"
    "- type: types text into the control that is already focused (click it first if needed).\n"
    "- press: one key or combo such as enter, esc, tab, ctrl+a, alt+left.\n"
    "- done: the goal is fully achieved and VISIBLE on screen. summary = one short sentence of what was done.\n"
    "- fail: impossible or the needed control is not visible. summary = why, in one short sentence.\n"
    "Never invent controls that are not visible. Do not repeat an action that had no effect; try another way or fail.")


def _type_text(text: str) -> None:
    import pyautogui
    if text.isascii():
        pyautogui.write(text, interval=0.01)
        return
    import tkinter as tk                                     # non-ASCII: paste through the clipboard
    root = tk.Tk(); root.withdraw(); root.clipboard_clear(); root.clipboard_append(text); root.update(); root.destroy()
    pyautogui.hotkey("ctrl", "v")


def screen_task(goal: str, max_steps: int = 5) -> tuple[bool, str]:
    """Achieve a GUI goal by repeatedly looking at the screen and acting. Returns (ok, spoken summary)."""
    import pyautogui
    history, last_sig = [], None
    for step in range(1, max_steps + 1):
        try:
            png, full = _capture()
        except Exception as exc:
            return False, f"I couldn't capture the screen: {exc}"
        data, err = _ask(_TASK_PROMPT.format(goal=goal, history="; ".join(history) or "none"), png)
        if err:
            return False, err
        if not isinstance(data, dict):
            return False, "I couldn't understand what I'm seeing on the screen."
        action = str(data.get("action", "")).lower()
        log.info("screen_task step %d: %s", step, data)
        if action == "done":
            return True, str(data.get("summary") or "Done.")[:200]
        if action == "fail":
            return False, str(data.get("summary") or "I couldn't do that on the screen.")[:200]
        sig = (action, str(data.get("box_2d")), str(data.get("text")), str(data.get("keys")))
        if sig == last_sig:
            return False, "I tried the same step twice and nothing changed, so I stopped."
        last_sig = sig
        try:
            if action in {"click", "double_click", "right_click"}:
                box = data.get("box_2d")
                if not (isinstance(box, list) and len(box) == 4):
                    return False, "I couldn't locate the control to click."
                x, y = _center(box, full)
                x, y = max(1, min(full[0] - 2, x)), max(1, min(full[1] - 2, y))
                {"click": pyautogui.click, "double_click": pyautogui.doubleClick, "right_click": pyautogui.rightClick}[action](x, y)
                history.append(f"{action} at {data.get('reason') or (x, y)}")
            elif action == "type":
                text = str(data.get("text") or "")[:500]
                if not text:
                    return False, "I had nothing to type."
                _type_text(text)
                history.append(f"typed '{text[:40]}'")
            elif action == "press":
                keys = str(data.get("keys") or "").lower().replace(" ", "")
                if keys not in SAFE_KEYS:
                    return False, f"I won't press {keys or 'that key'}; it isn't in my safe list."
                pyautogui.hotkey(*keys.split("+"))
                history.append(f"pressed {keys}")
            elif action == "scroll":
                pyautogui.scroll(-600 if str(data.get("direction")) == "down" else 600)
                history.append(f"scrolled {data.get('direction')}")
            elif action == "wait":
                history.append("waited")
            else:
                return False, f"I got an unsupported step from my vision model: {action or 'none'}."
        except Exception as exc:
            log.exception("screen_task action failed")
            return False, f"A screen action failed: {exc}"
        time.sleep(0.9)                                      # let the UI react before looking again
    return False, "I did several steps but I can't confirm the goal is finished. Please check the screen."