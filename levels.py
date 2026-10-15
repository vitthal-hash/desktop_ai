"""VAANI levels: exact volume / brightness control ("volume to 50 percent", "brightness max", "a bit louder").

parse_level(text, current) -> (target_percent, how) or None
    Understands: "to 50 percent", "50%", "fifty", "max/maximum/full", "min/minimum/lowest", "half",
    "by 20", "a bit / slightly / little" (5), "a lot / much" (25), plain up/down (10).
get_volume() / set_volume(pct)           Windows master volume (pycaw; falls back to volume keys, 2% each)
get_brightness() / set_brightness(pct)   screen-brightness-control
"""
from __future__ import annotations

import logging
import re
import sys

log = logging.getLogger("vaani.levels")

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

UP_WORDS = r"up|increase|raise|higher|louder|brighter|boost|more|add|bump"
DOWN_WORDS = r"down|decrease|lower|reduce|quieter|softer|dim|dimmer|less|drop|cut"


def words_to_number(text: str) -> str:
    """'volume to fifty percent' -> 'volume to 50 percent'; 'sixty five' -> '65'; 'a hundred' -> '100'."""
    t = text.lower()
    t = re.sub(r"\b(?:a|one) hundred\b", "100", t)
    t = re.sub(r"\b(" + "|".join(_TENS) + r")[\s-]+(" + "|".join(k for k, v in _UNITS.items() if 1 <= v <= 9) + r")\b",
               lambda m: str(_TENS[m.group(1)] + _UNITS[m.group(2)]), t)
    t = re.sub(r"\b(" + "|".join(_TENS) + r")\b", lambda m: str(_TENS[m.group(1)]), t)
    t = re.sub(r"\b(" + "|".join(_UNITS) + r")\b(?=\s*(?:%|percent|per cent)|\s*$)", lambda m: str(_UNITS[m.group(1)]), t)
    t = re.sub(r"\b(?<=to )(" + "|".join(_UNITS) + r")\b", lambda m: str(_UNITS[m.group(1)]), t)
    t = re.sub(r"\b(?<=by )(" + "|".join(_UNITS) + r")\b", lambda m: str(_UNITS[m.group(1)]), t)
    t = re.sub(r"\b(" + "|".join(_UNITS) + r")\b(?=\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)\b)", lambda m: str(_UNITS[m.group(1)]), t)
    return t


def _clamp(n) -> int:
    return max(0, min(100, int(round(n))))


def parse_level(text: str, current: int):
    """Return (target_percent, kind) where kind is 'set' | 'change', or None if the text has no usable direction/level."""
    t = words_to_number(text)
    if re.search(r"\b(?:max|maximum|maximize|full|highest|all the way up|top)\b", t) and not re.search(r"\b(?:min|minimum|lowest)\b", t):
        return 100, "set"
    if re.search(r"\b(?:min|minimum|minimize|lowest|bottom|all the way down)\b", t):
        return 0, "set"
    if re.search(r"\bhalf\b|\bfifty fifty\b", t):
        return 50, "set"
    if re.search(r"\b(?:quarter)\b", t):
        return 25, "set"
    m = re.search(r"(\d{1,3})\s*(?:%|percent|per cent)", t) or re.search(r"\b(?:to|at|on)\s+(\d{1,3})\b", t) \
        or re.search(r"\b(?:set|make)\b.*?\b(\d{1,3})\b", t)
    if m:
        return _clamp(int(m.group(1))), "set"
    up = re.search(rf"\b(?:{UP_WORDS})\b", t)
    down = re.search(rf"\b(?:{DOWN_WORDS})\b", t)
    if not up and not down:
        return None
    sign = 1 if (up and (not down or up.start() < down.start())) else -1
    m = re.search(r"\bby\s+(\d{1,3})\b", t)
    if m:
        step = int(m.group(1))
    elif re.search(r"\b(?:a bit|a little|slightly|little|tiny|touch|notch)\b", t):
        step = 5
    elif re.search(r"\b(?:a lot|much|way|lots|significantly|loads)\b", t):
        step = 25
    else:
        step = 10
    return _clamp(current + sign * step), "change"


# ---------------------------------------------------------------- volume
def _endpoint():
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    dev = AudioUtilities.GetSpeakers()
    ep = getattr(dev, "EndpointVolume", None)             # newer pycaw returns a wrapper that already has it
    if ep is not None:
        return ep
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


_assumed_volume = 50        # only used when pycaw is missing (we cannot read the real level then)


def get_volume() -> int:
    if sys.platform.startswith("win"):
        try:
            return _clamp(_endpoint().GetMasterVolumeLevelScalar() * 100)
        except Exception as exc:
            log.info("pycaw read failed (%s); assuming %s%%", exc, _assumed_volume)
    return _assumed_volume


def set_volume(pct: int) -> bool:
    """Exact master volume. Returns True when applied (pycaw) or approximated (key presses)."""
    global _assumed_volume
    pct = _clamp(pct)
    if sys.platform.startswith("win"):
        try:
            ep = _endpoint()
            if pct > 0:
                try:
                    ep.SetMute(0, None)
                except Exception:
                    pass
            ep.SetMasterVolumeLevelScalar(pct / 100.0, None)
            _assumed_volume = pct
            return True
        except Exception as exc:
            log.info("pycaw set failed (%s); using volume keys", exc)
    try:                                                    # fallback: each Windows volume key press = 2 %
        import pyautogui as pg
        pg.PAUSE = 0
        for _ in range(50):
            pg.press("volumedown")
        for _ in range(round(pct / 2)):
            pg.press("volumeup")
        _assumed_volume = pct
        return True
    except Exception:
        return False


def _volume_by_keys(pct: int) -> None:
    import pyautogui as pg
    pg.PAUSE = 0
    for _ in range(50):
        pg.press("volumedown")
    for _ in range(round(_clamp(pct) / 2)):
        pg.press("volumeup")


def _can_read_back(kind: str) -> bool:
    """False when the level is only assumed (no pycaw), so read-back would prove nothing."""
    if kind == "brightness":
        return True
    try:
        _endpoint().GetMasterVolumeLevelScalar()
        return True
    except Exception:
        return False


# ------------------------------------------------------------- brightness
def get_brightness() -> int:
    import screen_brightness_control as sbc
    cur = sbc.get_brightness()
    if isinstance(cur, (list, tuple)):
        cur = cur[0]
    return _clamp(cur)


def set_brightness(pct: int) -> int:
    import screen_brightness_control as sbc
    pct = _clamp(pct)
    sbc.set_brightness(pct)
    return pct


# ------------------------------------------------------------ one-call API
def adjust(kind: str, text: str):
    """kind = 'volume' | 'brightness'. Returns (ok, spoken_message)."""
    try:
        cur = get_volume() if kind == "volume" else get_brightness()
    except ImportError:
        return False, ("Brightness control needs screen-brightness-control installed." if kind == "brightness"
                       else "Volume control needs pycaw installed.")
    except Exception as exc:
        if kind == "brightness":
            return False, f"This display doesn't allow brightness control: {exc}"
        cur = 50
    parsed = parse_level(text, cur)
    if parsed is None:
        return False, f"Should I turn the {kind} up or down, or set it to a percent?"
    target, how = parsed
    try:
        if kind == "volume":
            ok = set_volume(target)
        else:
            set_brightness(target)
            ok = True
    except Exception as exc:
        return False, f"I couldn't change the {kind}: {exc}"
    if not ok:
        return False, f"I couldn't change the {kind}."
    # PROVE it: read the level back; if it didn't take, retry once the slow way
    try:
        import time
        time.sleep(0.15)
        actual = get_volume() if kind == "volume" else get_brightness()
        if abs(actual - target) > 4 and _can_read_back(kind):
            if kind == "volume":
                _volume_by_keys(target)
            else:
                import screen_brightness_control as sbc
                sbc.set_brightness(target)
            time.sleep(0.3)
            actual = get_volume() if kind == "volume" else get_brightness()
            if abs(actual - target) > 6:
                return False, f"I tried to set the {kind} to {target} percent, but it's still at {actual}."
            target = actual
    except Exception as exc:
        log.info("read-back skipped: %s", exc)
    if target == 100:
        return True, f"{kind.capitalize()} is at maximum."
    if target == 0:
        return True, f"{kind.capitalize()} is at minimum."
    return True, f"{kind.capitalize()} is at {target} percent."