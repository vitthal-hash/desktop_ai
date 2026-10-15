"""VAANI barge-in: say "stop" / "quiet" / "cancel" while VAANI is talking and it shuts up immediately.

Without echo cancellation a microphone also hears the speakers, so this is deliberately conservative:
  * it measures the echo level during the first moments of speech and only reacts to sound clearly LOUDER than that,
  * it only acts when the words are a stop phrase (interrupt.STOP_WORDS), never on anything else,
  * the global hotkey (Ctrl+Alt+X) always works as the dependable fallback.
Turn off with VAANI_BARGE_IN=0.  Use as:   with barge.watching(): voice.speak(...)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

import numpy as np

import interrupt

log = logging.getLogger("vaani.barge")


def enabled() -> bool:
    return os.getenv("VAANI_BARGE_IN", "1").strip().lower() not in {"0", "off", "false", "no"}


def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(a)))) if a is not None and len(a) else 0.0


def _watch(stop: threading.Event) -> None:
    try:
        import voice_input as vi
        model = vi._get_wake_model()
        with vi.Mic() as mic:
            baseline, started = 0.0, time.monotonic()
            buf, loud_since, quiet_since = [], None, None
            while not stop.is_set():
                chunk = mic.read(0.15)
                if chunk is None:
                    continue
                level = _rms(chunk)
                if time.monotonic() - started < 0.7:                    # learn how loud VAANI's own voice is at the mic
                    baseline = max(baseline, level)
                    continue
                gate = max(baseline * 2.4, 0.02)
                if level > gate:
                    buf.append(chunk)
                    loud_since = loud_since or time.monotonic()
                    quiet_since = None
                elif buf:
                    buf.append(chunk)
                    quiet_since = quiet_since or time.monotonic()
                    if time.monotonic() - quiet_since > 0.35:
                        audio = np.concatenate(buf)
                        buf, loud_since, quiet_since = [], None, None
                        if len(audio) / mic.rate < 0.25 or len(audio) / mic.rate > 4.0:
                            continue
                        text = vi.transcribe(model, vi.to_16k(audio, mic.rate), fast=True, english=True)
                        if text and interrupt.is_stop_phrase(text):
                            interrupt.cancel("spoken stop")
                            return
    except Exception as exc:
        log.info("barge-in watcher unavailable: %s", exc)


@contextmanager
def watching():
    """While inside the block, a spoken stop word interrupts the speech."""
    if not enabled():
        yield
        return
    stop = threading.Event()
    t = threading.Thread(target=_watch, args=(stop,), daemon=True, name="VAANI-BargeIn")
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(2.0)