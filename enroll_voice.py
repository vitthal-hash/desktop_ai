"""Teach VAANI your voice so it ignores everyone else.   python enroll_voice.py

  python enroll_voice.py            record 8 phrases, build your voiceprint, then test it
  python enroll_voice.py --test     check how well your voice (and a friend's) scores
  python enroll_voice.py --disable  turn the voice lock off      --enable  turn it back on
Tips: be in your normal usage spot, normal volume, normal speaking style. Re-run if it rejects you too often.
"""
import sys
import numpy as np

import speaker_lock as sl
import wakeword

PHRASES = [
    "Hey Computer.",
    "Hey Computer, open a new tab.",
    "Hey Computer, what is the time?",
    "Open the second video.",
    "Search for python tutorials on YouTube.",
    "Switch to the next tab and scroll down.",
    "Hey Computer, volume up.",
    "Select the third profile.",
]
PHRASES = [p.replace("Hey Computer", wakeword.display()) for p in PHRASES]


def record(prompt: str):
    import voice_input
    print(f'\n  Say:  "{prompt}"')
    while True:
        try:
            return voice_input.capture_utterance(max_wait=15, silence_end=1.0, max_len=10, verify=False)
        except TimeoutError:
            print("  (didn't hear anything - say it again)")


def test():
    if not sl.VOICEPRINT.exists():
        print("No voiceprint yet. Run: python enroll_voice.py")
        return
    print(f"Current threshold: {sl.threshold():.2f}   (match scores at or above it are accepted)")
    print("Say '" + wakeword.display() + ", open Chrome' 3 times yourself, then ask a friend to say it 3 times.")
    for who in ("YOU", "YOU", "YOU", "FRIEND", "FRIEND", "FRIEND"):
        input(f"\n[{who}] press Enter, then speak: ")
        audio = record(wakeword.display() + ", open Chrome")
        sc, secs = sl.score(audio)
        print(f"  {who}: score {sc:.2f}  ({secs:.1f}s of speech)  ->  {'ACCEPTED' if sc >= sl.threshold() else 'rejected'}")
    print("\nIf YOU are rejected: lower VAANI_VOICE_THRESHOLD (e.g. setx VAANI_VOICE_THRESHOLD 0.35) or re-enroll.")
    print("If your FRIEND is accepted: raise it (e.g. 0.55).")


def main():
    args = {a.lower() for a in sys.argv[1:]}
    if "--disable" in args:
        sl.DATA_DIR.mkdir(parents=True, exist_ok=True)
        sl.DISABLED_FLAG.write_text("off")
        print("Voice lock disabled. VAANI will answer to any voice.")
        return
    if "--enable" in args:
        sl.DISABLED_FLAG.unlink(missing_ok=True)
        print("Voice lock enabled.")
        return
    if "--test" in args:
        return test()
    print("Voice enrollment: I'll ask you to say 8 short phrases. Speak naturally.")
    print("Loading speech tools (the first time this downloads a 26 MB model)...")
    sl.ensure_model()
    clips = [record(p) for p in PHRASES]
    centroid, thr, own = sl.build_profile(clips)
    print(f"\nYour consistency scores: {', '.join(f'{x:.2f}' for x in own)}")
    sl.save_profile(centroid, thr)
    print(f"Saved. Match threshold set to {thr:.2f}.")
    if min(own) < 0.35:
        print("Some phrases matched weakly (noise or inconsistent speech). Consider re-running in a quieter spot.")
    print("\nNow run the check:  python enroll_voice.py --test   and restart VAANI.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")