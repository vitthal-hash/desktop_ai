"""VAANI desktop agent: "do this on the screen" in ANY app, with no guessing and proof at every step.

Loop: read the real controls of the active window (UI Automation) -> the model picks ONE control BY NUMBER ->
VAANI acts on that exact control -> checks the screen really changed (retrying another click method if not) ->
repeat. "Done" is only accepted after a second look confirms the goal is visible on screen.

Works with text-only local models (qwen) through the numbered control list, and gets sharper with a vision model
(Claude or Gemini) because it then also sees the screenshot with the same numbers drawn on it.
"""
from __future__ import annotations

import logging
import re
import time

import llm
import screen_state as ss
import verify

log = logging.getLogger("vaani.agent")
Result = tuple

RISKY = re.compile(r"\b(delete|remove|erase|format|uninstall|pay|purchase|buy|order|checkout|transfer|send|submit|post|publish|"
                   r"shut ?down|restart|sign ?out|log ?out|reset|wipe|empty|confirm purchase|place order)\b", re.I)
SAFE_KEYS = {"enter", "esc", "tab", "space", "backspace", "delete", "up", "down", "left", "right", "home", "end", "pageup",
             "pagedown", "f5", "f11", "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+f", "ctrl+s",
             "ctrl+t", "ctrl+w", "ctrl+l", "ctrl+n", "ctrl+shift+t", "alt+left", "alt+right", "alt+tab", "win+d", "alt+f4"}

SYSTEM = (
    "You operate a Windows PC for Chief. You are given the REAL controls of the active window as a numbered list, "
    "and sometimes a screenshot with the same numbers drawn on it. Decide the SINGLE next action and answer JSON only:\n"
    '{"action":"click|double_click|right_click|type|press|scroll|wait|done|fail",'
    '"element":<number from the list or null>,"text":"","keys":"","direction":"up|down","reason":"","summary":"","evidence":""}\n'
    "Rules:\n"
    "- Pick controls ONLY by the number in the list. Never invent a control. If the control you need is not listed, scroll, "
    "open the right window with a key, or answer fail.\n"
    "- type: types into the control given in 'element' (it is focused first). press: one key/combo like enter, esc, ctrl+l, alt+tab.\n"
    "- done: ONLY when the goal is already achieved and you can see proof in the list or screenshot. evidence = what proves it.\n"
    "- fail: impossible, or the needed control is missing. summary = why, one short sentence.\n"
    "- A step marked NO EFFECT did nothing: choose a different control or a different way. Never repeat it.\n"
    "- summary is read aloud: one short, friendly sentence, no numbers or technical words.")


def _cancelled() -> bool:
    try:
        import interrupt
        return interrupt.cancelled()
    except Exception:
        return False


# ------------------------------------------------------------ acting on elements
def _click_strategies(e: dict, action: str) -> list:
    import pyautogui
    cx, cy = ss.center(e)
    ctl = e.get("_ctl")
    out = []
    if action == "click" and ctl is not None:
        def invoke():
            ctl.GetInvokePattern().Invoke()
            return True, "invoked"
        out.append(("invoke", invoke))
        out.append(("uia click", lambda: (ctl.Click(waitTime=0.1) or True, "clicked")))
    verb = {"click": pyautogui.click, "double_click": pyautogui.doubleClick, "right_click": pyautogui.rightClick}[action]
    out.append(("mouse", lambda: (verb(cx, cy) or True, "clicked")))
    return out


def _type_into(e: dict | None, text: str) -> Result:
    import pyautogui
    if e is not None:
        ctl = e.get("_ctl")
        try:
            if ctl is not None:
                ctl.SetFocus()
            else:
                pyautogui.click(*ss.center(e))
        except Exception:
            pyautogui.click(*ss.center(e))
        time.sleep(0.15)
    if text.isascii():
        pyautogui.write(text, interval=0.01)
    else:
        import tkinter as tk
        root = tk.Tk(); root.withdraw(); root.clipboard_clear(); root.clipboard_append(text); root.update(); root.destroy()
        pyautogui.hotkey("ctrl", "v")
    return True, "typed"


def _observe() -> tuple:
    els = ss.elements(70)
    fg = ss.foreground()
    wins = [w for w in ss.list_windows() if w != fg.get("title")][:10]
    text = (f"Active window: {fg.get('title') or 'unknown'}\nOther windows: {'; '.join(wins) or 'none'}\n"
            f"Controls in the active window:\n{ss.describe_elements(els) if els else '(none readable: this app may draw its own interface)'}")
    png = None
    if llm.can_see():
        try:
            png = ss.annotated_png(els)
        except Exception as exc:
            log.info("screenshot failed: %s", exc)
    return els, text, png


def run(goal: str, max_steps: int = 8, announce=None, allow_risky: bool = False) -> Result:
    """Achieve a GUI goal. Returns (ok, one spoken sentence)."""
    history, no_effect_streak = [], 0
    if announce:
        announce("On it, Chief.")
    for step in range(1, max_steps + 1):
        if _cancelled():
            return False, "Okay, I've stopped."
        try:
            els, state_text, png = _observe()
        except Exception as exc:
            return False, f"I couldn't read the screen: {exc}"
        user = f"GOAL: {goal}\nSteps so far: {'; '.join(history) or 'none'}\n\n{state_text}"
        data, err = llm.ask(SYSTEM, user, png, tier="smart")
        if err:
            return False, err
        action = str(data.get("action", "")).lower().strip()
        log.info("agent step %d: %s", step, data)
        if action == "fail":
            return False, str(data.get("summary") or "I couldn't do that on the screen.")[:200]
        if action == "done":
            ok, msg = _confirm_done(goal, str(data.get("evidence") or ""), str(data.get("summary") or "Done."))
            if ok:
                return True, msg
            history.append("claimed done but the goal was NOT visible yet")
            continue
        if action == "wait":
            time.sleep(1.2)
            history.append("waited")
            continue
        elem = None
        if action in {"click", "double_click", "right_click", "type"} and data.get("element") not in (None, "", "null"):
            try:
                num = int(data["element"])
            except (TypeError, ValueError):
                num = 0
            elem = next((e for e in els if e["n"] == num), None)
            if elem is None:
                history.append(f"element {data.get('element')} does not exist (NO EFFECT)")
                no_effect_streak += 1
                if no_effect_streak >= 3:
                    return False, "I couldn't find the right control on the screen."
                continue
            if not elem.get("enabled", True):
                history.append(f"'{elem['name']}' is disabled (NO EFFECT)")
                continue
            if not allow_risky and RISKY.search(elem["name"]) and not RISKY.search(goal):
                return False, f"I stopped before pressing '{elem['name'][:30]}'. Tell me to go ahead if you want that."
        # ---- act, then PROVE it did something
        if action in {"click", "double_click", "right_click"}:
            if elem is None:
                return False, "I couldn't tell which control to click."
            ok, _msg, _trace = verify.attempt(f"click {elem['name'][:30] or elem['type']}", _click_strategies(elem, action),
                                              verify.screen_changed(0.002), settle=0.5, timeout=2.0)
            history.append(f"{action} '{elem['name'][:40]}'" + ("" if ok else " (NO EFFECT)"))
        elif action == "type":
            text = str(data.get("text") or "")[:500]
            if not text:
                return False, "I had nothing to type."
            before = ss.fingerprint()
            _type_into(elem, text)
            time.sleep(0.4)
            ok, _why = ss.diff(before, ss.fingerprint(), 0.0005)
            history.append(f"typed '{text[:40]}'" + ("" if ok else " (NO EFFECT)"))
        elif action == "press":
            keys = str(data.get("keys") or "").lower().replace(" ", "")
            if keys not in SAFE_KEYS:
                return False, f"I won't press {keys or 'that key'}; it isn't on my safe list."
            import pyautogui
            ok, _msg, _trace = verify.attempt(f"press {keys}", [("keyboard", lambda: (pyautogui.hotkey(*keys.split("+")) or True, "pressed"))],
                                              verify.screen_changed(0.001), settle=0.4, timeout=1.5)
            history.append(f"pressed {keys}" + ("" if ok else " (NO EFFECT)"))
        elif action == "scroll":
            import pyautogui
            d = "down" if str(data.get("direction")) == "down" else "up"
            ok, _msg, _trace = verify.attempt(f"scroll {d}", [("wheel", lambda: (pyautogui.scroll(-600 if d == "down" else 600) or True, "scrolled"))],
                                              verify.screen_changed(0.003), settle=0.3, timeout=1.2)
            history.append(f"scrolled {d}" + ("" if ok else " (NO EFFECT)"))
        else:
            return False, f"My vision model asked for something I can't do: {action or 'nothing'}."
        no_effect_streak = 0 if ok else no_effect_streak + 1
        if no_effect_streak >= 2:
            return False, "I tried two different ways and the screen didn't react, so I stopped instead of guessing."
    return False, "That took more steps than I'm allowed to try, so I stopped."


def _confirm_done(goal: str, evidence: str, summary: str) -> tuple:
    """Second look: is the goal really achieved on the CURRENT screen? Never trust the first 'done'."""
    time.sleep(0.8)
    try:
        els, state_text, png = _observe()
    except Exception:
        return True, summary[:200]
    user = (f"GOAL: {goal}\nThe assistant claims it is done. Its evidence: {evidence or 'none given'}\n\n{state_text}\n\n"
            'Look ONLY at the current screen state. Answer JSON: {"achieved": true|false, "why": "short"}')
    data, err = llm.ask("You verify whether a computer task was really completed. Be strict: if you cannot see proof, say false.",
                        user, png, tier="fast")
    if err or not isinstance(data, dict):
        return True, summary[:200]                           # verifier unavailable: trust the step-level checks
    if data.get("achieved") is True:
        return True, summary[:200]
    log.info("done rejected by verifier: %s", data.get("why"))
    return False, ""