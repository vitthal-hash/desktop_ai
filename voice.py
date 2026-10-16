"""VAANI speech output: loud, clear, with natural pauses.

speak(text) blocks until finished. It cleans text for speech (no 'asterisk', no URLs read out),
splits it into phrases and pauses between them (longer after full stops/questions, shorter after commas).

Backends (VAANI_TTS env var):
  auto  (default)  Windows System.Speech through PowerShell (one fresh process per reply = reliable, even under pythonw),
                   and if that fails, pyttsx3. Every failure is written to vaani.log.
  ps               PowerShell only        pyttsx3   pyttsx3 only
  edge             Microsoft neural voices through the edge-tts package (needs internet, nicer voice)
Other settings:  VAANI_VOICE (part of a voice name, e.g. "Zira" or "Heera"; for edge a full name e.g. "en-IN-NeerjaNeural").
                 Default: the first installed FEMALE voice (Zira, Heera, Hazel...).
                 VAANI_TTS_RATE (words/min, default 175)   VAANI_TTS_VOLUME (0.0-1.0, default 1.0)
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger("vaani.voice")

RATE = int(os.getenv("VAANI_TTS_RATE", "175"))
VOLUME = max(0.0, min(1.0, float(os.getenv("VAANI_TTS_VOLUME", "1.0"))))
def _default_backend() -> str:
    """Natural neural voice (edge) when the package is installed, otherwise the Windows voice."""
    try:
        import edge_tts  # noqa: F401
        return "edge"
    except Exception:
        return "auto"


BACKEND = (os.getenv("VAANI_TTS", "") or "").strip().lower() or _default_backend()      # auto | ps | pyttsx3 | edge
if BACKEND not in {"auto", "ps", "pyttsx3", "edge", "kokoro"}:
    BACKEND = _default_backend()                  # old values like "system" now mean: use the best available voice
EDGE_RATE = os.getenv("VAANI_EDGE_RATE", "+4%")   # a touch quicker than default sounds more natural
EDGE_PITCH = os.getenv("VAANI_EDGE_PITCH", "+0Hz")
VOICE_PREF = os.getenv("VAANI_VOICE", "").strip()

PAUSE_AFTER = {".": 0.38, "!": 0.40, "?": 0.42, ";": 0.25, ":": 0.25, ",": 0.14, "\n": 0.50}
MAX_CHUNK = 220

_stop = threading.Event()
_proc = None
_engine = None
_lock = threading.Lock()


# ----------------------------------------------------------------- text prep
def clean_for_speech(text: str) -> str:
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)                      # code blocks are not read aloud
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]+\)", r"\1", t)       # [text](url) -> text
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", t, flags=re.M)         # bullets / numbering
    t = re.sub(r"\*\*|__", "", t)                                       # bold markers (opening AND closing)
    t = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)\*(?!\w)", "", t)             # stray italic markers
    t = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]", "", t)   # emoji
    t = t.replace("&", " and ").replace("%", " percent").replace("→", " to ").replace("…", ".")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def split_chunks(text: str) -> list:
    """-> [(phrase, pause_seconds_after)]"""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<=[.!?;:,])\s+", line)
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            pause = PAUSE_AFTER.get(part[-1], 0.0)
            if i == len(parts) - 1:                                      # end of a line / list item: breathe
                pause = max(pause, 0.35)
            while len(part) > MAX_CHUNK:                                # very long phrase: break at a space
                cut = part.rfind(" ", 0, MAX_CHUNK)
                cut = cut if cut > 40 else MAX_CHUNK
                out.append((part[:cut].strip(), 0.08))
                part = part[cut:].strip()
            if part:
                out.append((part, pause))
    # merge tiny fragments ("Dr." / "No.") into the next phrase so they aren't spoken alone
    merged = []
    for chunk, pause in out:
        if merged and len(merged[-1][0]) < 4 and merged[-1][1] <= 0.4:
            merged[-1] = (merged[-1][0] + " " + chunk, pause)
        else:
            merged.append((chunk, pause))
    return merged


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not _stop.is_set():
        time.sleep(0.03)


# ------------------------------------------------------------- SAPI backend
FEMALE_PREFERENCE = ("zira", "heera", "hazel", "susan", "aria", "jenny", "sonia", "catherine", "linda", "female")


def _pick_voice(voices, preference: str):
    """Chosen voice id: your VAANI_VOICE text if set, otherwise the first installed female voice."""
    names = [(v.id, f"{v.name or ''} {v.id or ''}".lower()) for v in voices]
    wanted = [preference.lower()] if preference else list(FEMALE_PREFERENCE)
    for w in wanted:
        for vid, text in names:
            if w in text:
                return vid
    return None


def _get_engine():
    global _engine
    if _engine is None:
        import pyttsx3
        _engine = pyttsx3.init()
        _engine.setProperty("rate", RATE)
        _engine.setProperty("volume", VOLUME)
        vid = _pick_voice(_engine.getProperty("voices"), VOICE_PREF)
        if vid:
            _engine.setProperty("voice", vid)
        log.info("speech voice: %s", vid or "system default")
    return _engine


def _say_pyttsx3(chunk: str) -> None:
    global _engine
    try:
        eng = _get_engine()
        eng.say(chunk)
        eng.runAndWait()
    except Exception:
        _engine = None                                                    # rebuild next time
        raise


_PS_SCRIPT = r"""
[Console]::InputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$chunks = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Volume = __VOLUME__
$s.Rate = __RATE__
$pref = '__VOICE__'
try {
  if ($pref -ne '') {
    $v = $s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name } | Where-Object { $_ -like "*$pref*" } | Select-Object -First 1
    if ($v) { $s.SelectVoice($v) }
  } else {
    $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)
  }
} catch { }
$pb = New-Object System.Speech.Synthesis.PromptBuilder
foreach ($c in $chunks) {
  $pb.AppendText([string]$c.t)
  if ([int]$c.p -gt 0) { $pb.AppendBreak([TimeSpan]::FromMilliseconds([int]$c.p)) }
}
$s.Speak($pb)
"""


def _say_powershell(chunks: list) -> None:
    """Speak a whole reply (phrases + pauses) in ONE PowerShell/System.Speech process. Raises on failure."""
    global _proc
    if not sys.platform.startswith("win"):
        raise RuntimeError("PowerShell speech only works on Windows")
    import json
    rate = max(-10, min(10, round((RATE - 175) / 12)))
    script = (_PS_SCRIPT.replace("__VOLUME__", str(int(VOLUME * 100))).replace("__RATE__", str(rate))
              .replace("__VOICE__", VOICE_PREF.replace("'", "")))
    payload = json.dumps([{"t": c, "p": int(p * 1000)} for c, p in chunks]).encode("utf-8")
    proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=0x08000000)
    _proc = proc
    try:
        _, err = proc.communicate(payload, timeout=120)
    finally:
        _proc = None
    if proc.returncode not in (0, None) and not _stop.is_set():
        raise RuntimeError("PowerShell speech exited with code %s: %s" % (proc.returncode, (err or b"").decode("utf-8", "replace")[:300]))


# --------------------------------------------------------------- edge backend
_edge_seq = 0
_reach = {"t": 0.0, "ok": False}


def _edge_reachable() -> bool:
    """Cheap internet check (1.5 s, cached for a minute) so an offline PC skips the neural voice instantly."""
    import socket
    if time.time() - _reach["t"] < 60:
        return _reach["ok"]
    try:
        socket.create_connection(("speech.platform.bing.com", 443), timeout=1.5).close()
        _reach.update(t=time.time(), ok=True)
    except OSError:
        _reach.update(t=time.time(), ok=False)
    return _reach["ok"]


def _mci(cmd: str) -> None:
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    rc = ctypes.windll.winmm.mciSendStringW(cmd, buf, 255, 0)
    if rc != 0:
        err = ctypes.create_unicode_buffer(256)
        ctypes.windll.winmm.mciGetErrorStringW(rc, err, 255)
        raise RuntimeError(f"audio player error {rc}: {err.value}")


def _say_edge(chunk: str) -> None:                                        # needs: pip install edge-tts
    """Neural voice. Raises (so the next backend takes over) on ANY problem: offline, no file, player error."""
    global _edge_seq
    import asyncio
    import tempfile
    import edge_tts
    if not sys.platform.startswith("win"):
        raise RuntimeError("edge playback is Windows-only here")
    if not _edge_reachable():
        raise RuntimeError("no internet route to the neural voice service")
    voice = VOICE_PREF if "-" in VOICE_PREF else os.getenv("VAANI_EDGE_VOICE", "en-IN-NeerjaNeural")
    _edge_seq += 1
    path = os.path.join(tempfile.gettempdir(), f"vaani_tts_{os.getpid()}_{_edge_seq}.mp3")
    try:
        comm = edge_tts.Communicate(chunk, voice, rate=EDGE_RATE, pitch=EDGE_PITCH)
    except TypeError:
        comm = edge_tts.Communicate(chunk, voice)

    async def go():
        await asyncio.wait_for(comm.save(path), timeout=12)
    asyncio.run(go())
    if not os.path.exists(path) or os.path.getsize(path) < 800:
        raise RuntimeError("the neural voice returned no audio")
    try:
        try:
            _mci("close vaani")
        except RuntimeError:
            pass
        _mci(f'open "{path}" type mpegvideo alias vaani')
        _mci(f"setaudio vaani volume to {int(VOLUME * 1000)}")
        _mci("play vaani wait")
    finally:
        try:
            _mci("close vaani")
        except RuntimeError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass


_FAIL: dict = {}                    # backend name -> time.time() until which it is skipped
_told: set = set()


def _backend_order() -> list:
    """Which voices to try, best first. Anything that failed recently is skipped for a while."""
    chains = {"edge": ["edge", "ps", "pyttsx3"], "kokoro": ["kokoro", "edge", "ps", "pyttsx3"],
              "auto": ["ps", "pyttsx3"], "ps": ["ps"], "pyttsx3": ["pyttsx3"]}
    names = chains.get(BACKEND, ["ps", "pyttsx3"])
    if not sys.platform.startswith("win"):
        names = [n for n in names if n != "ps"]
    now = time.time()
    ready = [n for n in names if _FAIL.get(n, 0) <= now]
    return ready or names


def _mark_failed(name: str, exc: Exception) -> None:
    _FAIL[name] = time.time() + (300 if name in {"edge", "kokoro"} else 45)
    log.warning("voice backend %s failed: %s: %s", name, type(exc).__name__, str(exc)[:200])
    if name not in _told:
        _told.add(name)
        label = {"edge": "neural", "kokoro": "Kokoro", "ps": "Windows", "pyttsx3": "backup"}.get(name, name)
        print(f"[VAANI] The {label} voice didn't work ({str(exc)[:90]}). Trying another voice.", flush=True)


def _run_backend(name: str, pending: list) -> None:
    """Speak what is left in `pending` (list of (text, pause)); remove items as they finish."""
    if name == "ps":
        _say_powershell(pending)
        pending.clear()
    elif name == "kokoro":
        _speak_kokoro(pending)
        pending.clear()
    elif name == "edge":
        pending[:] = _merge_for_edge(pending)
        while pending and not _stop.is_set():
            text, _pause = pending[0]
            _say_edge(text)
            pending.pop(0)
    else:                                                                  # pyttsx3
        while pending and not _stop.is_set():
            text, pause = pending[0]
            _say_pyttsx3(text)
            pending.pop(0)
            if pause:
                _sleep(pause)


def _merge_for_edge(chunks: list) -> list:
    """The neural voice handles its own pauses, so send whole sentences in one go (smoother, less robotic)."""
    merged, cur = [], ""
    for chunk, _pause in chunks:
        if cur and len(cur) + len(chunk) > 450:
            merged.append((cur, 0.0))
            cur = ""
        cur = (cur + " " + chunk).strip()
    if cur:
        merged.append((cur, 0.0))
    return merged


_kokoro = None
_last = {"text": "", "t": 0.0}


def last_spoken() -> tuple:
    """(text VAANI last said, time.monotonic() when it finished/started) - used to ignore its own echo."""
    return _last["text"], _last["t"]


def _speak_kokoro(chunks: list) -> None:
    """Offline neural voice (pip install kokoro soundfile; needs espeak-ng for some words). Interruptible."""
    global _kokoro
    import numpy as np
    import sounddevice as sd
    if _kokoro is None:
        from kokoro import KPipeline
        _kokoro = KPipeline(lang_code=os.getenv("VAANI_KOKORO_LANG", "a"))
    voice = os.getenv("VAANI_KOKORO_VOICE", "af_heart")
    speed = float(os.getenv("VAANI_KOKORO_SPEED", "1.0"))
    for chunk, _pause in chunks:
        if _stop.is_set():
            return
        for _gs, _ps, audio in _kokoro(chunk, voice=voice, speed=speed):
            if _stop.is_set():
                return
            sd.play(np.asarray(audio, dtype="float32"), 24000)
            while sd.get_stream().active and not _stop.is_set():
                time.sleep(0.03)
            if _stop.is_set():
                sd.stop()
                return


# ----------------------------------------------------------------- public API
def speak(text: str) -> None:
    """Say it out loud. Tries the best voice first and falls back automatically; only raises if EVERY voice fails."""
    clean = clean_for_speech(text)
    if not clean:
        return
    pending = split_chunks(clean)
    _last["text"], _last["t"] = clean, time.monotonic()          # set early too: the echo starts while we are still talking
    with _lock:
        _stop.clear()
        log.info("speaking (%s): %s", BACKEND, clean[:120].replace("\n", " "))
        last_exc = None
        for name in _backend_order():
            if _stop.is_set() or not pending:
                break
            try:
                _run_backend(name, pending)
                last_exc = None
                break
            except Exception as exc:
                if _stop.is_set():
                    break
                last_exc = exc
                _mark_failed(name, exc)
        if last_exc is not None and pending:
            log.error("SPEECH FAILED on every voice - nothing was said")
            raise RuntimeError(f"no voice worked: {last_exc}")
        _last["text"], _last["t"] = clean, time.monotonic()
        time.sleep(0.2)                                                    # let the speaker tail die before the mic opens


def stop_speaking() -> None:
    _stop.set()
    try:
        import sounddevice as sd
        sd.stop()
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.winmm.mciSendStringW("stop vaani", None, 0, 0)
    except Exception:
        pass
    try:
        if _proc is not None:
            _proc.terminate()
        if _engine is not None:
            _engine.stop()
    except Exception:
        pass


def test_voices() -> None:
    """python voice.py   ->  tries every voice one by one and says which ones work on THIS PC."""
    print("Testing each voice. Listen, and note which ones you can hear.\n")
    results = {}
    for name, label in (("ps", "Windows voice"), ("edge", "Neural voice (needs internet)"), ("pyttsx3", "Backup voice"), ("kokoro", "Kokoro offline neural voice")):
        if name == "ps" and not sys.platform.startswith("win"):
            continue
        _stop.clear()
        pending = [(f"This is the {label}. Can you hear me, Chief?", 0.0)]
        try:
            _run_backend(name, pending)
            results[name] = "worked (did you HEAR it?)"
        except Exception as exc:
            results[name] = f"FAILED: {type(exc).__name__}: {str(exc)[:120]}"
        print(f"  {label:36} {results[name]}")
    print("\nPut the best one in .env:  VAANI_TTS=edge   (neural, needs internet)   or   VAANI_TTS=ps   (Windows voice, offline)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_voices()