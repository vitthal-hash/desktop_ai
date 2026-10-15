"""VAANI model layer: one function to ask a model, with automatic provider choice and a fast/smart tier.

ask(system, user, image_png=None, tier="smart", want_json=True) -> (data_or_text, error)

Providers (first one with a key wins, override with VAANI_LLM=anthropic|gemini|ollama):
  anthropic : ANTHROPIC_API_KEY   smart=VAANI_SMART_MODEL (default claude-sonnet-5-5)   fast=VAANI_FAST_MODEL (claude-haiku-5-5)
  gemini    : GEMINI_API_KEY      smart=GEMINI_SMART_MODEL (gemini-3.5-flash)           fast=GEMINI_MODEL (gemini-3.1-flash-lite)
  ollama    : local, text-only    VAANI_MODEL (qwen2.5:3b-instruct); images only if VAANI_OLLAMA_VISION is set (e.g. qwen2.5vl:7b)
Model names are just defaults: put whatever your account offers in .env.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re

import env_loader  # noqa: F401

log = logging.getLogger("vaani.llm")


def provider() -> str:
    forced = os.getenv("VAANI_LLM", "").strip().lower()
    if forced in {"anthropic", "gemini", "ollama"}:
        return forced
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if os.getenv("GEMINI_API_KEY", "").strip():
        return "gemini"
    return "ollama"


def can_see() -> bool:
    """True when the active provider can look at screenshots."""
    p = provider()
    return p in {"anthropic", "gemini"} or bool(os.getenv("VAANI_OLLAMA_VISION", "").strip())


def _parse_json(raw: str):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _anthropic(system, user, image_png, tier, want_json):
    import anthropic
    model = os.getenv("VAANI_SMART_MODEL" if tier == "smart" else "VAANI_FAST_MODEL", "").strip() or \
        ("claude-sonnet-5-5" if tier == "smart" else "claude-haiku-5-5")
    content = []
    if image_png:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": base64.b64encode(image_png).decode()}})
    content.append({"type": "text", "text": user + ("\nReply with a single JSON object and nothing else." if want_json else "")})
    client = anthropic.Anthropic(timeout=40.0)
    r = client.messages.create(model=model, max_tokens=900, temperature=0, system=system,
                               messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def _gemini(system, user, image_png, tier, want_json):
    from google import genai
    from google.genai import types
    names = [os.getenv("GEMINI_SMART_MODEL", "gemini-3.5-flash") if tier == "smart" else os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
             os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"), "gemini-3.5-flash"]
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", "").strip(), http_options=types.HttpOptions(timeout=40000))
    parts = ([types.Part.from_bytes(data=image_png, mime_type="image/png")] if image_png else []) + [user]
    cfg = {"temperature": 0, "system_instruction": system}
    if want_json:
        cfg["response_mime_type"] = "application/json"
    last = None
    for model in dict.fromkeys(n for n in names if n):
        try:
            r = client.models.generate_content(model=model, config=cfg, contents=parts)
            if (r.text or "").strip():
                return r.text
        except Exception as exc:
            last = exc
            log.warning("gemini %s failed: %s", model, str(exc)[:140])
    raise RuntimeError(f"Gemini did not answer ({type(last).__name__ if last else 'empty'})")


def _ollama(system, user, image_png, tier, want_json):
    import ollama
    vision = os.getenv("VAANI_OLLAMA_VISION", "").strip()
    model = vision if (image_png and vision) else os.getenv("VAANI_MODEL", "qwen2.5:3b-instruct")
    msg = {"role": "user", "content": user}
    if image_png and vision:
        msg["images"] = [image_png]
    kw = {"format": "json"} if want_json else {}
    r = ollama.chat(model=model, messages=[{"role": "system", "content": system}, msg], keep_alive="30m",
                    options={"temperature": 0, "num_predict": 500, "num_ctx": 6144}, **kw)
    return r.message.content or ""


def ask(system: str, user: str, image_png: bytes | None = None, tier: str = "smart", want_json: bool = True):
    """Returns (data, error). data is a dict/list when want_json else a string."""
    p = provider()
    if image_png and not can_see():
        image_png = None                                    # text-only provider: caller must cope (UIA text mode)
    try:
        raw = {"anthropic": _anthropic, "gemini": _gemini, "ollama": _ollama}[p](system, user, image_png, tier, want_json)
    except ImportError as exc:
        return None, f"The {p} package isn't installed ({exc.name}). Run: pip install {'anthropic' if p == 'anthropic' else 'google-genai' if p == 'gemini' else 'ollama'}"
    except Exception as exc:
        log.warning("%s call failed: %s: %s", p, type(exc).__name__, str(exc)[:200])
        return None, f"The {p} model request failed ({type(exc).__name__})."
    if not want_json:
        return (raw or "").strip(), None
    data = _parse_json(raw)
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None, "The model's answer wasn't valid JSON."
    return data, None