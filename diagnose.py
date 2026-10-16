"""VAANI diagnostics. Run from the project folder:

    python diagnose.py voice     tries every voice and tells you which ones work on this PC
    python diagnose.py scroll    tests each scroll method on the window you leave in front, and shows which one works
    python diagnose.py mic       shows your microphone level so you can see whether it hears you
    python diagnose.py all
"""
from __future__ import annotations

import sys
import time


def voice() -> None:
    import voice as v
    v.test_voices()


def scroll() -> None:
    import scrolling as sc
    print("\nOpen a LONG web page (for example a YouTube search or a Wikipedia article) and leave it in front.")
    for n in range(8, 0, -1):
        print(f"  starting in {n}...", end="\r", flush=True)
        time.sleep(1)
    print("\nTesting each scroll method on its own (the page should move DOWN a few times):\n")
    for name, fn in (("wheel message", lambda: sc.method_wheel_message("down")), ("mouse wheel", lambda: sc.method_mouse_wheel("down")),
                     ("keyboard (Page Down)", lambda: sc.method_keyboard("down"))):
        try:
            before = sc.snapshot()
            fn()
            time.sleep(0.8)
            after = sc.snapshot()
            moved, shift, change = sc.vertical_shift(before, after)
            print(f"  {name:22} -> {'MOVED the page' if moved else 'did NOT move the page'}  (screen change {change:.1f}, shift {shift} rows)")
        except Exception as exc:
            print(f"  {name:22} -> error: {type(exc).__name__}: {exc}")
        time.sleep(0.5)
    print("\nSend me this output and I'll fix the method that failed. Whichever says MOVED is what VAANI will use.")


def mic() -> None:
    import numpy as np
    import sounddevice as sd
    print("\nSpeak normally for 6 seconds. You should see the bar grow when you talk.\n")
    rate = 16000
    with sd.InputStream(samplerate=rate, channels=1, dtype="float32") as stream:
        end = time.time() + 6
        while time.time() < end:
            data, _ = stream.read(int(rate * 0.2))
            level = float(np.sqrt(np.mean(np.square(data))))
            print(f"  level {level:0.4f} " + "#" * min(60, int(level * 600)), flush=True)
    print("\nIf the bar stays empty while you talk, Windows is using the wrong microphone or it is muted.")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"voice": voice, "scroll": scroll, "mic": mic}
    for name in (steps if what == "all" else [what]):
        if name in steps:
            print(f"\n=== {name} ===")
            steps[name]()