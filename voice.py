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
if BACKEND not in {"auto", "ps", "pyttsx3", "edge"}:
    BACKEND = _default_backend()                  # old values like "system" now mean: use the best available voice
EDGE_RATE = os.getenv("VAANI_EDGE_RATE", "+4%")   # a touch quicker than default sounds more natural
EDGE_PITCH = os.getenv("VAANI_EDGE_PITCH", "+0Hz")
VOICE_PREF = os.getenv("VAANI_VOICE", "").strip()

PAUSE_AFTER = {".": 0.38, "!": 0.40, "?": 0.42, ";": 0.25, ":": 0.25, ",": 0.14, "\n": 0.50}
MAX_CHUNK = 220

_stop = threading.Event()
_proc = None
_engine = None
_edge_failed_until = 0.0
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
def _say_edge(chunk: str) -> None:                                        # needs: pip install edge-tts
    import asyncio, tempfile
    import edge_tts
    voice = VOICE_PREF if "-" in VOICE_PREF else "en-IN-NeerjaNeural"      # female (Indian English)
    path = os.path.join(tempfile.gettempdir(), f"vaani_tts_{os.getpid()}.mp3")
    try:
        comm = edge_tts.Communicate(chunk, voice, rate=EDGE_RATE, pitch=EDGE_PITCH)
    except TypeError:
        comm = edge_tts.Communicate(chunk, voice)
    asyncio.run(comm.save(path))
    import ctypes
    mci = ctypes.windll.winmm.mciSendStringW
    mci('close vaani', None, 0, 0)
    mci(f'open "{path}" type mpegvideo alias vaani', None, 0, 0)
    mci(f'setaudio vaani volume to {int(VOLUME * 1000)}', None, 0, 0)
    mci('play vaani wait', None, 0, 0)
    mci('close vaani', None, 0, 0)


def _speak_chunk(chunk: str) -> None:
    """One phrase through edge (if selected) or pyttsx3."""
    global _edge_failed_until
    if BACKEND == "edge" and time.time() >= _edge_failed_until:
        try:
            return _say_edge(chunk)
        except Exception as exc:
            log.warning("edge-tts failed (%s); using the offline voice for a while", exc)
            _edge_failed_until = time.time() + 120
    _say_pyttsx3(chunk)


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


def _speak_chunks_one_by_one(chunks: list) -> None:
    if BACKEND == "edge" and time.time() >= _edge_failed_until:
        chunks = _merge_for_edge(chunks)
    for chunk, pause in chunks:
        if _stop.is_set():
            break
        _speak_chunk(chunk)
        if pause and not _stop.is_set():
            _sleep(pause)


# ----------------------------------------------------------------- public API
def speak(text: str) -> None:
    clean = clean_for_speech(text)
    if not clean:
        return
    chunks = split_chunks(clean)
    with _lock:
        _stop.clear()
        log.info("speaking (%s): %s", BACKEND, clean[:120].replace("\n", " "))
        try:
            if BACKEND in ("auto", "ps") and sys.platform.startswith("win"):
                try:
                    _say_powershell(chunks)
                except Exception as exc:
                    log.error("PowerShell speech failed: %s", exc)
                    if BACKEND == "ps":
                        raise
                    log.info("falling back to pyttsx3")
                    _speak_chunks_one_by_one(chunks)
            else:
                _speak_chunks_one_by_one(chunks)
        except Exception:
            log.exception("SPEECH FAILED - nothing was said")
            raise
        time.sleep(0.2)                                                    # let the speaker tail die before the mic opens


def stop_speaking() -> None:
    _stop.set()
    try:
        if _proc is not None:
            _proc.terminate()
        if _engine is not None:
            _engine.stop()
    except Exception:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    speak("Good evening, Chief. What are your orders for me? I can open apps, control Chrome, and answer questions.")