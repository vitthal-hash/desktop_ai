"""Find out exactly which speech path works on THIS PC.   python voice_test.py
Run it in the VAANI folder with the venv active. Answer y/n after each sound. Then tell me the summary at the end."""
import subprocess, sys, traceback

import voice


def heard(q):
    return input(f"   -> {q} (y/n): ").strip().lower().startswith("y")


results = {}
print("\n=== VAANI speech test ===  (turn your volume up; make sure the right speakers are selected)\n")

print("Installed Windows voices:")
try:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
                          "ForEach-Object { $_.VoiceInfo.Name + ' | ' + $_.VoiceInfo.Gender + ' | ' + $_.VoiceInfo.Culture }"],
                         capture_output=True, text=True, timeout=30)
    print(out.stdout.strip() or "(none found)  " + out.stderr.strip()[:200])
except Exception as e:
    print("   could not list voices:", e)

print("\n[1] PowerShell / System.Speech (the new default)")
try:
    voice._say_powershell([("Test one. This is the PowerShell voice.", 300), ("If you hear this, Chief, it works.", 0)])
    results["powershell"] = heard("Did you hear BOTH sentences clearly?")
except Exception:
    traceback.print_exc()
    results["powershell"] = False

print("\n[2] pyttsx3 (the old method), two sentences in a row")
try:
    voice._say_pyttsx3("Test two. This is the pyttsx3 voice.")
    voice._say_pyttsx3("This is the second sentence from pyttsx3.")
    results["pyttsx3"] = heard("Did you hear BOTH sentences?")
except Exception:
    traceback.print_exc()
    results["pyttsx3"] = False

print("\n[3] The real VAANI speech function, with pauses")
try:
    voice.speak("Good evening, Chief. What's the order for me? I'm opening a new tab now. Done.")
    results["voice.speak"] = heard("Did you hear the whole message, with small pauses?")
except Exception:
    traceback.print_exc()
    results["voice.speak"] = False

print("\n=== SUMMARY ===")
for k, v in results.items():
    print(f"  {'WORKS ' if v else 'SILENT'}  {k}")
if results["voice.speak"]:
    print("\nSpeech works from this terminal. If VAANI is still silent when started by the scheduled task,")
    print("restart it (Stop-ScheduledTask / kill pythonw / Start-ScheduledTask) and send me the last lines of vaani.log.")
elif results["powershell"] and not results["pyttsx3"]:
    print('\nRun:  setx VAANI_TTS ps    then restart VAANI.')
elif results["pyttsx3"] and not results["powershell"]:
    print('\nRun:  setx VAANI_TTS pyttsx3    then restart VAANI.')
else:
    print("\nNothing produced sound. Check: Windows volume/mute, the selected output device, and that Windows Speech voices exist")
    print("(Settings > Time & language > Speech). Send me the whole output of this test.")