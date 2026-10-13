"""Voice lock: VAANI only reacts to the enrolled owner's voice.

Setup once:  python enroll_voice.py      (about 1 minute, reads 8 short phrases)
Needs:       pip install sherpa-onnx      (the ~26 MB speaker model downloads automatically on first use)
Turn off:    python enroll_voice.py --disable      Turn on again: python enroll_voice.py --enable
Tuning:      VAANI_VOICE_THRESHOLD (0.2-0.8). Higher = stricter. Default = value measured during enrollment.

Honest limits: this is voice *recognition*, not bank-grade security. A recording of your voice, or a very similar
voice, can pass. If verification itself breaks, VAANI fails OPEN (keeps working) and logs a warning.
"""
from __future__ import annotations

import logging
import os
import threading
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("vaani.speaker")

DATA_DIR = Path(os.getenv("VAANI_DATA_DIR", str(Path.home() / "AppData" / "Local" / "VAANI")))
VOICEPRINT = DATA_DIR / "voiceprint.npz"
DISABLED_FLAG = DATA_DIR / "voicelock.disabled"
MODEL_PATH = DATA_DIR / "models" / "wespeaker_en_voxceleb_resnet34.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_resnet34.onnx")
SAMPLE_RATE = 16000
MIN_SPEECH_SECONDS = 0.6
DEFAULT_THRESHOLD = 0.45

_lock = threading.Lock()
_extractor = None
_profile = None          # (embedding, threshold) once loaded
_warned = False


def ensure_model() -> Path:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("[VAANI] Downloading the voice-recognition model (26 MB, one time)...", flush=True)
    tmp = MODEL_PATH.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


def _get_extractor():
    global _extractor
    with _lock:
        if _extractor is None:
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(ensure_model()), num_threads=1, debug=False, provider="cpu")
            if not cfg.validate():
                raise RuntimeError("speaker model config is invalid")
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return _extractor


def speech_only(audio: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Drop silence so the embedding is computed on speech only."""
    audio = np.asarray(audio, dtype=np.float32)
    frame = int(rate * 0.02)
    n = len(audio) // frame
    if n == 0:
        return audio
    frames = audio[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    cutoff = max(0.008, 0.25 * float(np.percentile(rms, 90)))
    keep = frames[rms >= cutoff]
    return keep.reshape(-1) if keep.size else audio[:0]


def embed(audio_16k: np.ndarray) -> np.ndarray:
    ex = _get_extractor()
    stream = ex.create_stream()
    stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=np.asarray(audio_16k, dtype=np.float32))
    stream.input_finished()
    e = np.array(ex.compute(stream), dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def _load_profile():
    global _profile
    if _profile is None and VOICEPRINT.exists():
        data = np.load(VOICEPRINT)
        _profile = (data["embedding"].astype(np.float32), float(data["threshold"]))
    return _profile


def is_enabled() -> bool:
    if os.getenv("VAANI_VOICE_LOCK", "").strip().lower() in {"0", "off", "false"} or DISABLED_FLAG.exists():
        return False
    try:
        return _load_profile() is not None
    except Exception:
        return False


def threshold() -> float:
    env = os.getenv("VAANI_VOICE_THRESHOLD", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    prof = _load_profile()
    return prof[1] if prof else DEFAULT_THRESHOLD


def score(audio_16k: np.ndarray) -> tuple:
    """(cosine_score, speech_seconds). Raises on model errors."""
    speech = speech_only(audio_16k)
    secs = len(speech) / SAMPLE_RATE
    if secs < MIN_SPEECH_SECONDS:
        return 0.0, secs
    emb = embed(speech)
    ref = _load_profile()[0]
    return float(np.dot(emb, ref)), secs


def verify(audio_16k: np.ndarray) -> tuple:
    """-> (accepted: bool, score: float). Fails open on internal errors."""
    global _warned
    if not is_enabled():
        return True, 1.0
    try:
        sc, secs = score(audio_16k)
        if secs < MIN_SPEECH_SECONDS:
            log.info("voice lock: only %.2fs of speech - too short to verify, ignoring", secs)
            return False, 0.0
        ok = sc >= threshold()
        log.info("voice lock: score %.2f (need %.2f) -> %s", sc, threshold(), "ACCEPT" if ok else "REJECT")
        return ok, sc
    except Exception:
        if not _warned:
            log.exception("voice lock failed; continuing WITHOUT it")
            _warned = True
        return True, 1.0


def build_profile(utterances: list) -> tuple:
    """utterances: list of 16 kHz float32 arrays. Returns (centroid, threshold, per_sample_scores)."""
    embs = [embed(speech_only(u)) for u in utterances if len(speech_only(u)) >= MIN_SPEECH_SECONDS * SAMPLE_RATE]
    if len(embs) < 3:
        raise ValueError("Not enough clear speech was recorded (need at least 3 usable phrases).")
    centroid = np.mean(embs, axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-9
    own = []                                                             # leave-one-out similarity
    for i, e in enumerate(embs):
        others = np.mean([x for j, x in enumerate(embs) if j != i], axis=0)
        others /= np.linalg.norm(others) + 1e-9
        own.append(float(np.dot(e, others)))
    thr = max(0.30, min(0.55, min(own) - 0.06))
    return centroid, thr, own


def save_profile(centroid: np.ndarray, thr: float) -> None:
    global _profile
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(VOICEPRINT, embedding=centroid.astype(np.float32), threshold=np.float32(thr))
    DISABLED_FLAG.unlink(missing_ok=True)
    _profile = (centroid.astype(np.float32), float(thr))
