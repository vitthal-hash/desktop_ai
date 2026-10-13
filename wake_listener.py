"""Wake phrase detector: waits for 'Hey Computer' (optionally followed by a command) from the OWNER only.

Utterance based (fast + light): it only runs speech recognition after you finish a phrase, instead of
transcribing every 0.8 s. If voice lock is enrolled, other people's voices are ignored.
"""
from __future__ import annotations

import difflib
import logging
import re

from vaani_control import ListeningPaused, is_paused, shutdown_event
import voice_input

log = logging.getLogger("vaani.wake")
WAKE_PHRASE = "Hey Computer"
_GREETINGS = {"hey", "hay", "hi", "hello", "okay", "ok", "a", "hate", "he"}


def wake_model_ready():
    print("[VAANI] Preparing speech recognition models...", flush=True)
    voice_input._get_wake_model()
    voice_input._get_model()
    import speaker_lock
    if speaker_lock.is_enabled():
        try:
            speaker_lock._get_extractor()
            print("[VAANI] Voice lock is ON: I only respond to the enrolled voice.", flush=True)
        except Exception as exc:
            print(f"[VAANI] Voice lock could not load ({exc}); continuing without it.", flush=True)
    else:
        print("[VAANI] Voice lock is OFF (run enroll_voice.py to make me respond only to you).", flush=True)
    print(f'[VAANI] Wake detector ready. Say "{WAKE_PHRASE}".', flush=True)


def _words(text: str) -> list:
    return re.findall(r"[a-z]+", text.lower())


def _wake_result(transcript: str):
    """False when absent; True for just the phrase; the command text when one follows it."""
    words = _words(transcript)
    if len(words) < 2 or words[0] not in _GREETINGS:
        return False
    if difflib.SequenceMatcher(None, words[1], "computer").ratio() < 0.78:
        return False
    if len(words) == 2:
        return True
    rest = " ".join(words[2:])
    return rest if rest else True


def wait_for_wake_word():
    """Blocks until the wake phrase is heard. Returns True, or the command text spoken with it.
    Raises ListeningPaused when the mic is switched off; RuntimeError if the microphone dies."""
    import speaker_lock
    model = voice_input._get_wake_model()
    print(f'[VAANI] Listening for "{WAKE_PHRASE}"...', flush=True)
    with voice_input.Mic() as mic:
        det = voice_input.UtteranceDetector(mic.rate, silence_end=0.55, min_speech=0.3, max_len=8.0)
        stalls = 0
        while True:
            if shutdown_event().is_set():
                return False
            if is_paused():
                raise ListeningPaused()
            chunk = mic.read(0.5)
            if chunk is None:
                stalls += 1
                if stalls >= 12:
                    raise RuntimeError("The microphone stopped delivering audio (device asleep or unplugged).")
                continue
            stalls = 0
            utt = det.feed(chunk)
            if utt is None:
                continue
            audio = voice_input.to_16k(utt, mic.rate)
            text = voice_input.transcribe(model, audio, fast=True, english=voice_input.WAKE_MODEL.endswith(".en"))
            if not text:
                continue
            result = _wake_result(text)
            log.info("heard %r -> wake=%s", text, bool(result))
            if not result:
                continue
            ok, sc = speaker_lock.verify(audio)
            if not ok:
                print(f"[VAANI] Wake phrase from a voice that isn't yours was ignored (match {sc:.2f}).", flush=True)
                continue
            print("[VAANI] Wake phrase detected!", flush=True)
            return result
