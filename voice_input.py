"""VAANI speech input (Faster-Whisper): microphone capture, utterance detection, transcription.

Accuracy settings (env): VAANI_STT_MODEL (default small.en; try distil-large-v3 on a GPU or a strong CPU),
VAANI_WAKE_MODEL (default tiny.en), VAANI_END_SILENCE (quiet that ends a command, default 0.9 s; a sentence that
stops on a dangling word like "and" / "to" / "open" automatically waits for the rest), VAANI_BEAM (default 5).
Use VAANI_STT_MODEL=small (multilingual) if you speak Hindi or other languages to VAANI.
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
STT_MODEL = os.getenv("VAANI_STT_MODEL", "small.en")
WAKE_MODEL = os.getenv("VAANI_WAKE_MODEL", "tiny.en")
END_SILENCE = float(os.getenv("VAANI_END_SILENCE", "0.9"))
BEAM = max(1, int(os.getenv("VAANI_BEAM", "5")))
# Teaches Whisper the words Chief actually says, so "git hub" -> GitHub, "whats app" -> WhatsApp, "volume" not "all you".
PROMPT = os.getenv("VAANI_STT_PROMPT", (
    "Voice commands for a computer assistant: open Chrome, new tab, close the current window, switch to the GitHub tab, "
    "YouTube, WhatsApp, Gmail, Claude, ChatGPT, scroll down, scroll up, volume to 50 percent, brightness to maximum, "
    "play a song, open the second video, click the first result, go back, search for, take a screenshot, minimize, maximize."))
# a sentence that ends on one of these is obviously not finished yet
_DANGLING = {"and", "then", "to", "the", "a", "an", "on", "in", "of", "for", "with", "at", "by", "from", "my", "or", "but",
             "because", "into", "onto", "set", "make", "turn", "open", "search", "type", "write", "click", "switch",
             "find", "send", "tell", "call", "message", "number", "than"}

MIN_THRESHOLD = float(os.getenv("VAANI_MIN_THRESHOLD", "0.006"))   # lower = catches quiet / fast starts of words
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
                 max_len: float = 15.0, preroll: float = 0.6):
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


def _prepare(audio: np.ndarray) -> np.ndarray:
    """Remove DC offset and bring quiet speech up to a healthy level (never amplify noise by more than 8x)."""
    a = np.asarray(audio, dtype=np.float32)
    if a.size == 0:
        return a
    a = a - float(np.mean(a))
    peak = float(np.max(np.abs(a)))
    if 1e-4 < peak < 0.5:
        a = a * min(8.0, 0.7 / peak)
    return np.clip(a, -1.0, 1.0).astype(np.float32)


def transcribe(model, audio_16k: np.ndarray, fast: bool = True, english: bool = True) -> str:
    """fast=True (wake word): quickest decode. fast=False (commands): beam search + vocabulary prompt for clean words."""
    kwargs = dict(language="en" if english else None, condition_on_previous_text=False,
                  no_speech_threshold=0.6, compression_ratio_threshold=2.4, vad_filter=True,
                  vad_parameters=dict(threshold=0.35, min_silence_duration_ms=600, speech_pad_ms=400))
    if fast:
        segments, _ = model.transcribe(audio_16k, beam_size=1, **kwargs)
    else:
        hints = ""
        try:
            import vocab
            hints = ", ".join(vocab.hint_words())[:300]
        except Exception:
            pass
        prompt = (PROMPT + " " + hints).strip()
        try:                                              # hotwords nudges Whisper toward YOUR names (newer faster-whisper)
            segments, _ = model.transcribe(_prepare(audio_16k), beam_size=BEAM, initial_prompt=prompt,
                                           hotwords=hints or None, **kwargs)
        except TypeError:
            segments, _ = model.transcribe(_prepare(audio_16k), beam_size=BEAM, initial_prompt=prompt, **kwargs)
    text = " ".join(s.text.strip() for s in segments).strip()
    if text.lower().strip(" .!?,") in HALLUCINATIONS:
        return ""
    if not fast and PROMPT and text.strip().lower() in PROMPT.lower() and len(text.split()) > 6:
        return ""                                   # Whisper sometimes just echoes its own prompt on silence
    return text


def _unfinished(text: str) -> bool:
    """True when the sentence clearly stopped mid-thought ('open the', 'volume to', 'and')."""
    words = [w for w in text.lower().replace(",", " ").replace(".", " ").split() if w]
    return bool(words) and words[-1] in _DANGLING and not text.rstrip().endswith(("?", "!"))


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
    english = STT_MODEL.endswith(".en")
    text = transcribe(model, audio, fast=False, english=english)
    if not text:
        return None
    for _ in range(2):                              # natural speech has pauses: "open the ... second video"
        if not _unfinished(text):
            break
        try:
            more = capture_utterance(max_wait=2.5, silence_end=END_SILENCE, verify=False)
        except (TimeoutError, ListeningPaused):
            break
        extra = transcribe(model, more, fast=False, english=english)
        if not extra:
            break
        text = (text.rstrip(" .,") + " " + extra).strip()
    try:
        import vocab
        fixed = vocab.apply(text)                         # "tmqc" -> "Taarak Mehta Ka Ooltah Chashmah"
        if fixed != text:
            print(f"[VAANI vocabulary] {text}  ->  {fixed}", flush=True)
        text = fixed
    except Exception:
        pass
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