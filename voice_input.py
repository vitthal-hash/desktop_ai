"""VAANI speech input (Faster-Whisper): microphone capture, utterance detection, transcription.

Speed settings (env): VAANI_STT_MODEL (default base.en), VAANI_WAKE_MODEL (default tiny.en),
VAANI_END_SILENCE (seconds of quiet that ends a command, default 0.8).
Use VAANI_STT_MODEL=base (multilingual) if you speak Hindi or other languages to VAANI.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from math import gcd

import numpy as np

from vaani_control import ListeningPaused, is_paused, shutdown_event

log = logging.getLogger("vaani.stt")

TARGET_RATE = 16000
BLOCK = 1024
STT_MODEL = os.getenv("VAANI_STT_MODEL", "base.en")
WAKE_MODEL = os.getenv("VAANI_WAKE_MODEL", "tiny.en")
END_SILENCE = float(os.getenv("VAANI_END_SILENCE", "0.8"))
MIN_THRESHOLD = 0.010
HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "bye", "okay", "ok", "so", "the", "uh", "um", "hmm", "mm"}

_models: dict = {}
_model_lock = threading.Lock()


def _load(name: str):
    with _model_lock:
        if name not in _models:
            from faster_whisper import WhisperModel
            print(f"[VAANI] Preparing speech recognition model ({name}); first run may download it.", flush=True)
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 4) // 2))
        return _models[name]


def _get_model():
    return _load(STT_MODEL)


def _get_wake_model():
    return _load(WAKE_MODEL)


def refresh_audio() -> None:
    """Re-initialise PortAudio after sleep / device changes so a dead microphone handle is replaced."""
    try:
        import sounddevice as sd
        sd._terminate()
        sd._initialize()
        log.info("audio subsystem re-initialised")
    except Exception:
        log.exception("could not re-initialise audio")


class Mic:
    """Context manager around a sounddevice input stream; read() returns float32 mono chunks."""

    def __init__(self):
        self.rate = TARGET_RATE
        self._q: queue.Queue = queue.Queue(maxsize=600)
        self._stream = None

    def __enter__(self):
        import sounddevice as sd
        try:
            self.rate = int(sd.query_devices(kind="input")["default_samplerate"])
        except Exception as exc:
            raise RuntimeError("No microphone found. Check Windows Settings > Sound > Input.") from exc
        self._stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="float32", blocksize=BLOCK, callback=self._cb)
        self._stream.start()
        return self

    def _cb(self, indata, frames, t, status):
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def read(self, timeout: float = 0.5):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def __exit__(self, *exc):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass


class UtteranceDetector:
    """Feed audio chunks; returns the audio of a finished utterance (speech followed by silence)."""

    def __init__(self, rate: int, silence_end: float = END_SILENCE, min_speech: float = 0.25,
                 max_len: float = 15.0, preroll: float = 0.35):
        self.rate, self.silence_end, self.min_speech, self.max_len = rate, silence_end, min_speech, max_len
        self.preroll_chunks = max(1, int(preroll * rate / BLOCK))
        self.floor = 0.003
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=self.preroll_chunks)
        self.buf: list = []
        self.in_speech = False
        self.speech_n = 0
        self.silence_s = 0.0
        self.total_n = 0

    def threshold(self) -> float:
        return max(MIN_THRESHOLD, self.floor * 3.0)

    def feed(self, chunk: np.ndarray):
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0
        dur = len(chunk) / self.rate
        loud = rms >= self.threshold()
        if not self.in_speech:
            self.floor = 0.97 * self.floor + 0.03 * min(rms, 0.05)       # learn the room's noise level
            if not loud:
                self.pre.append(chunk)
                return None
            self.in_speech, self.buf = True, list(self.pre) + [chunk]
            self.pre.clear()
            self.speech_n, self.silence_s, self.total_n = len(chunk), 0.0, len(chunk)
            return None
        self.buf.append(chunk)
        self.total_n += len(chunk)
        if loud:
            self.speech_n += len(chunk)
            self.silence_s = 0.0
        else:
            self.silence_s += dur
        if self.silence_s >= self.silence_end or self.total_n / self.rate >= self.max_len:
            audio = np.concatenate(self.buf)
            long_enough = self.speech_n / self.rate >= self.min_speech
            self.reset()
            return audio if long_enough else None
        return None


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    if rate == TARGET_RATE:
        return audio
    from scipy.signal import resample_poly
    g = gcd(rate, TARGET_RATE)
    return resample_poly(audio, TARGET_RATE // g, rate // g).astype(np.float32)


def transcribe(model, audio_16k: np.ndarray, fast: bool = True, english: bool = True) -> str:
    segments, _ = model.transcribe(audio_16k, language="en" if english else None,
                                   beam_size=1 if fast else 3, vad_filter=True, condition_on_previous_text=False,
                                   no_speech_threshold=0.6, compression_ratio_threshold=2.4)
    text = " ".join(s.text.strip() for s in segments).strip()
    if text.lower().strip(" .!?,") in HALLUCINATIONS:
        return ""
    return text


def capture_utterance(max_wait=None, silence_end: float = END_SILENCE, max_len: float = 15.0, verify: bool = True):
    """Block until the owner finishes speaking. Returns 16 kHz audio.
    Raises TimeoutError (nobody started speaking within max_wait seconds) or ListeningPaused."""
    import speaker_lock
    check_voice = verify and speaker_lock.is_enabled()
    with Mic() as mic:
        det = UtteranceDetector(mic.rate, silence_end=silence_end, max_len=max_len)
        waited, stalls = 0.0, 0
        while True:
            if shutdown_event().is_set() or is_paused():
                raise ListeningPaused()
            chunk = mic.read(0.5)
            if chunk is None:
                stalls += 1
                if stalls >= 12:
                    raise RuntimeError("The microphone stopped delivering audio.")
                continue
            stalls = 0
            if not det.in_speech:
                waited += len(chunk) / mic.rate
                if max_wait is not None and waited >= max_wait:
                    raise TimeoutError("no speech")
            utt = det.feed(chunk)
            if utt is None:
                continue
            audio = to_16k(utt, mic.rate)
            if check_voice:
                ok, sc = speaker_lock.verify(audio)
                if not ok:
                    print(f"[VAANI] Ignored a voice that isn't yours (match {sc:.2f}).", flush=True)
                    continue
            return audio


def listen_once(auto_stop: bool = False, max_wait_seconds=None):
    """Return the recognised text (None if nothing intelligible).
    auto_stop=True: automatic start/stop on silence. False: push-to-talk with Enter."""
    model = _get_model()
    if not auto_stop:
        return _push_to_talk(model)
    audio = capture_utterance(max_wait=max_wait_seconds)
    text = transcribe(model, audio, fast=True, english=STT_MODEL.endswith(".en"))
    if not text:
        return None
    print(f"[VAANI recognised] {text}", flush=True)
    return text


def _push_to_talk(model):
    import sounddevice as sd
    chunks = []
    rate = int(sd.query_devices(kind="input")["default_samplerate"])
    input("\nPress Enter to start recording...")
    with sd.InputStream(samplerate=rate, channels=1, dtype="float32", callback=lambda i, f, t, s: chunks.append(i[:, 0].copy())):
        input("Recording - press Enter to stop.")
    if not chunks:
        return None
    return transcribe(model, to_16k(np.concatenate(chunks), rate), fast=False, english=STT_MODEL.endswith(".en")) or None


if __name__ == "__main__":
    print("Say something...")
    print(listen_once(auto_stop=True, max_wait_seconds=10))
