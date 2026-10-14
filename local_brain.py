"""VAANI local brain: the same planner as gemini_planner, but running on your own qwen model through Ollama.

Used automatically when GEMINI_API_KEY is not set (or Gemini fails). Same prompt, same allow-list validation,
so a small model can never run anything outside the safe action list.

plan_steps(user_text, context) -> (steps, ask, error)      never raises
Model: VAANI_BRAIN_MODEL (default: the same model as the chat, qwen2.5:3b-instruct)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import gemini_planner as gp

log = logging.getLogger("vaani.localbrain")
MODEL = os.getenv("VAANI_BRAIN_MODEL", os.getenv("VAANI_MODEL", "qwen2.5:3b-instruct")).strip() or "qwen2.5:3b-instruct"
KEEP_ALIVE = os.getenv("VAANI_KEEP_ALIVE", "30m")
_SPLIT_AT = "CONTEXT (JSON):"


def available() -> bool:
    if os.getenv("VAANI_NO_BRAIN") or os.getenv("VAANI_NO_LOCAL_BRAIN"):
        return False
    try:
        import ollama  # noqa: F401
        return True
    except ImportError:
        return False


def _split_prompt() -> tuple:
    head = gp.PROMPT.split(_SPLIT_AT)[0].rstrip()
    return head + "\nAnswer with the JSON object only, nothing else."


def _parse(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None


def plan_steps(user_text: str, context: dict | None = None) -> tuple:
    """-> (steps, ask, error)"""
    started = time.monotonic()
    try:
        import ollama
        resp = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": _split_prompt()},       # identical every time -> Ollama reuses its cache
                {"role": "user", "content": _SPLIT_AT + "\n" + json.dumps(context or {}, ensure_ascii=False)
                 + '\nChief said: "' + user_text.replace('"', "'") + '"'},
            ],
            format="json",
            keep_alive=KEEP_ALIVE,
            options={"temperature": 0, "num_predict": 220, "num_ctx": 4096},
        )
        raw = (resp.message.content or "").strip()
    except Exception as exc:
        log.warning("local brain call failed: %s: %s", type(exc).__name__, str(exc)[:160])
        return [], "", f"Local model request failed ({type(exc).__name__})."
    log.info("local brain %dms (%s): %s", int((time.monotonic() - started) * 1000), MODEL, raw[:400])
    data = _parse(raw)
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return [], "", "The local brain's answer wasn't valid JSON."
    ask = str(data.get("ask") or "").strip()[:200]
    steps, err = gp.validate_steps(data.get("steps", []))
    if err:
        log.warning("local plan rejected: %s | %s", err, raw[:300])
        return [], "", err
    if not steps and not ask:
        return [], "", "The local brain didn't produce any action."
    return steps, ask, None