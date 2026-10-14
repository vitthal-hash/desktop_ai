"""VAANI hands-free assistant with a Windows system-tray control icon."""
from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import logging
from pathlib import Path
import pc_control
import friendly
import memory

import ollama
from tools import perform_action
from voice import speak
from voice_input import listen_once
from wake_listener import wait_for_wake_word, wake_model_ready
import voice_input
from vaani_control import (
    ListeningPaused, consume_resume, is_paused, request_pause, request_shutdown,
    set_status, shutdown_event, single_instance, start_power_watch,
)
from tray_status import start_tray, stop_tray

MODEL = os.getenv("VAANI_MODEL", "qwen2.5:3b-instruct")
WAKE_PHRASE = "Hey Computer"
SESSION_IDLE_TIMEOUT_SECONDS = 30
# These end the multi-command session but retain wake-word detection.
SLEEP_PHRASES = {
    "go to sleep", "sleep now", "stop listening", "end session",
    "that's all", "thats all", "goodbye vaani", "bye vaani",
}
# These turn off all microphone capture; click Resume to reactivate.
PAUSE_PHRASES = {
    "pause microphone", "turn microphone off", "disable microphone",
    "pause all listening", "stop microphone",
}
SYSTEM_PROMPT = """
You are VAANI, Chief's personal assistant and friend, like a warm, quick-witted companion sitting beside him.
Address the user as Chief. Your replies are spoken aloud, so talk the way a kind person talks: short, natural,
relaxed sentences, contractions, no markdown, lists, emojis or URLs. Usually one or two sentences; three at most
unless Chief asks for detail.
Notice how Chief feels. If he sounds sad, stressed, tired or frustrated, respond to the feeling first, gently,
before anything else. If he is happy, be happy with him. Never lecture, never sound like a robot or a manual,
never say "as an AI". If you did not understand, ask one short friendly question instead of guessing.
Computer actions are performed by the Python action layer, not by your text. Never claim an action succeeded
unless the action layer confirms it. If something is unsupported, say so honestly and suggest what you can do.
When you mention a website or tab, use its simple name (GitHub, YouTube), never a long title or address.
""".strip()
messages = [{"role": "system", "content": SYSTEM_PROMPT}]


def _configure_logging():
    try:
        log_dir = Path.home() / "AppData" / "Local" / "VAANI"
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(log_dir / "vaani.log"), level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
        )
    except Exception:
        pass


def answer_question(user_text: str, mood: str | None = None) -> str:
    messages.append({"role": "user", "content": user_text})
    try:
        call_messages = list(messages)
        hint = friendly.mood_hint(mood) if mood else ""
        if hint:                                          # only for this one call, not kept in the history
            call_messages.append({"role": "system", "content": hint})
        response = ollama.chat(model=MODEL, messages=call_messages, keep_alive="30m",
                               options={"temperature": 0.5, "num_predict": 220})
        answer = (response.message.content or "").strip() or "I couldn't generate a response, Chief."
        messages.append({"role": "assistant", "content": answer})
        logging.info("CHAT: %s => %s", user_text, answer)
        return answer
    except Exception:
        if messages and messages[-1].get("role") == "user":
            messages.pop()
        logging.exception("Ollama request failed")
        return "Chief, I couldn't connect to Ollama. Check that Ollama is running and " + MODEL + " is installed."


def handle_request(user_text: str) -> str:
    mood = friendly.detect_mood(user_text)
    if mood:
        logging.info("MOOD: %s", mood)
    try:                                               # follow-ups: "more", "again", "play this song" after opening YouTube
        followup = memory.resolve(user_text)
    except Exception:
        logging.exception("memory.resolve failed")
        followup = None
    if followup:
        kind, value = followup
        if kind == "ask":
            logging.info("FOLLOWUP ask: %s => %s", user_text, value)
            return value
        logging.info("FOLLOWUP: %r understood as %r", user_text, value)
        user_text = value
    result = pc_control.handle(user_text)              # NEW: apps, media, WhatsApp, UIA clicks, multi-step
    if result is None:
        result = perform_action(user_text)             # your existing router (unchanged)
        result = pc_control.rescue(user_text, result)  # NEW: screen agent if router says "unsupported"
    if result is not None:
        success, detail = result
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": detail})
        logging.info("ACTION %s: %s => %s", "OK" if success else "FAILED", user_text, detail)
        try:
            memory.note(user_text, success, detail)
        except Exception:
            logging.exception("memory.note failed")
        if mood and success and mood != "happy":
            return friendly.empathy(mood) + " " + detail      # "Take a breath, Chief. We'll get through it. Volume is at 30 percent."
        return detail
    return answer_question(user_text, mood)



def _normalized(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .!?\t\r\n")


def _strip_wake_prefix(text: str) -> str:
    import re
    return re.sub(r"^\s*hey[\s,.-]+computer[\s,.:;-]*", "", text, flags=re.I).strip()


def greeting() -> str:
    hour = datetime.datetime.now().hour
    if 5 <= hour < 12:
        part = "Good morning"
    elif 12 <= hour < 17:
        part = "Good afternoon"
    elif 17 <= hour < 22:
        part = "Good evening"
    else:
        part = "Good evening"
    return f"{part}, Chief. What's the order for me?"


def _warm_up_ollama() -> None:
    """Load the chat model into memory in the background so the first answer is fast."""
    def run():
        try:
            ollama.chat(model=MODEL, messages=[{"role": "user", "content": "hi"}], keep_alive="30m",
                        options={"num_predict": 1})
            logging.info("Ollama warmed up")
        except Exception:
            logging.warning("Ollama warm-up failed (is Ollama running?)")
    threading.Thread(target=run, daemon=True, name="VAANI-OllamaWarmup").start()


def _say(text: str):
    try:
        speak(text)
    except Exception:
        logging.exception("Speech output failed")


def process_one_request(user_text: str) -> str:
    user_text = user_text.strip()
    if not user_text:
        return ""
    logging.info("HEARD: %s", user_text)
    set_status("processing", "Understanding your command")
    try:
        reply = handle_request(user_text)
    except Exception:
        logging.exception("handle_request crashed")
        reply = "Sorry, Chief, something went wrong with that one. Please try again."
    print(f"\nVAANI: {reply}\n", flush=True)
    set_status("speaking", "Speaking a response")
    _say(reply)
    return reply


def run_command_session(first_command: str | None = None) -> None:
    """Accept commands after one wake phrase until sleep, pause, or idle timeout."""
    print("\n[VAANI] Session active. No need to repeat the wake phrase.", flush=True)
    if first_command and first_command.strip():
        command = _strip_wake_prefix(first_command)
        if command:
            if _normalized(command) in PAUSE_PHRASES:
                request_pause(True)
                _say("Microphone paused, Chief. Click the VAANI microphone icon in the Windows notification area when you want me back.")
                return
            if _normalized(command) in SLEEP_PHRASES:
                _say("I'll return to wake-word mode, Chief. Say Hey Computer when you need me.")
                return
            process_one_request(command)
    while not shutdown_event().is_set() and not is_paused():
        try:
            set_status("command", "Listening for your command")
            command = listen_once(auto_stop=True, max_wait_seconds=SESSION_IDLE_TIMEOUT_SECONDS)
        except ListeningPaused:
            return
        except TimeoutError:
            logging.info("Command session ended by idle timeout")
            print(f"[VAANI] Session idle for {SESSION_IDLE_TIMEOUT_SECONDS}s. Returning to wake-word mode.", flush=True)
            return
        except KeyboardInterrupt:
            raise
        except Exception as error:
            logging.exception("Command session microphone error")
            set_status("error", "Microphone error — check the VAANI log")
            print(f"[VAANI] Microphone issue: {error}", flush=True)
            time.sleep(1)
            return
        if not command:
            continue
        command = _strip_wake_prefix(command)
        normalized = _normalized(command)
        if normalized in PAUSE_PHRASES:
            # Speak before setting pause so the response can finish using audio output.
            set_status("speaking", "Pausing microphone")
            _say("Microphone paused, Chief. Click the VAANI microphone icon in the Windows notification area when you want me back.")
            request_pause(True)
            return
        if normalized in SLEEP_PHRASES:
            set_status("speaking", "Returning to wake-word mode")
            _say("Returning to wake-word mode, Chief. Say Hey Computer when you need me.")
            logging.info("Command session ended by spoken stop/sleep command")
            return
        process_one_request(command)


def _wait_while_paused():
    if is_paused():
        set_status("paused", "Microphone paused — click Resume to reactivate")
    while is_paused() and not shutdown_event().is_set():
        time.sleep(0.25)
    if not shutdown_event().is_set():
        set_status("wake", 'Waiting for "Hey Computer"')


def run_wake_mode() -> None:
    try:
        from browser_bridge import start_server
        start_server()
        logging.info("VAANI browser bridge started on 127.0.0.1:8765")
        print("[VAANI] Browser bridge ready on 127.0.0.1:8765", flush=True)
    except Exception:
        logging.exception("Browser bridge failed to start")
        print("[VAANI] Browser bridge could not start; browser control may be unavailable.", flush=True)
    start_power_watch()
    print("\n[VAANI] Hands-free mode is running.", flush=True)
    print(f'Say "{WAKE_PHRASE}" to start. Say "stop listening" to go back to waiting; use the VAANI tray menu to pause or resume the microphone.', flush=True)
    logging.info("VAANI hands-free service started")
    failures = 0
    try:
        wake_model_ready()
        while not shutdown_event().is_set():
            _wait_while_paused()
            if shutdown_event().is_set():
                break
            if consume_resume():
                logging.info("Resumed from sleep: refreshing the microphone")
                voice_input.refresh_audio()
                set_status("speaking", "Welcome back")
                _say("Welcome back, Chief. I'm listening.")
            try:
                set_status("wake", 'Listening for "Hey Computer"')
                detected = wait_for_wake_word()
                failures = 0
            except ListeningPaused:
                continue
            except KeyboardInterrupt:
                raise
            except Exception as error:
                # Microphone vanished (sleep, unplugged, driver reset): recover instead of dying.
                failures += 1
                logging.exception("Wake listening failed (attempt %d)", failures)
                set_status("recovering", "Microphone problem - retrying")
                print(f"[VAANI] Microphone problem ({error}); retrying...", flush=True)
                voice_input.refresh_audio()
                time.sleep(min(2 * failures, 15))
                continue
            if is_paused() or shutdown_event().is_set():
                continue
            if not detected:
                continue
            set_status("command", "Wake phrase detected - listening for command")
            if isinstance(detected, str):
                run_command_session(detected)
            else:
                try:
                    first_command = listen_once(auto_stop=True, max_wait_seconds=12)
                except ListeningPaused:
                    continue
                except TimeoutError:
                    print("[VAANI] No command heard after wake phrase. Returning to wake-word mode.", flush=True)
                    continue
                except Exception as error:
                    logging.exception("First command microphone error")
                    set_status("recovering", "Microphone problem - retrying")
                    print(f"[VAANI] Couldn't hear the command: {error}", flush=True)
                    voice_input.refresh_audio()
                    time.sleep(1)
                    continue
                run_command_session(first_command)
    except KeyboardInterrupt:
        logging.info("VAANI stopped with keyboard interrupt")
        print("\n[VAANI] Hands-free mode stopped.", flush=True)
    except Exception:
        logging.exception("Fatal hands-free error")
        set_status("error", "VAANI error - check the VAANI log")
        print("[VAANI] A fatal error occurred. See %LOCALAPPDATA%\\VAANI\\vaani.log.", flush=True)
        raise                                                   # lets the supervisor / Task Scheduler restart us


def run_console() -> None:
    print("VAANI text console. Commands: /wake, /reset, /exit")
    while True:
        try:
            text = input("\nChief: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if text.lower() in {"/exit", "/quit"}:
            return
        if text.lower() == "/reset":
            messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("VAANI: Conversation history cleared, Chief.")
            continue
        if text.lower() == "/wake":
            run_wake_mode()
            continue
        process_one_request(text)


def _kill_old_instances() -> None:
    """Stop any older VAANI still running (e.g. the previous version with its own tray icon) before starting this one."""
    try:
        import subprocess
        me = os.getpid()
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -match 'main\\.py' "
              f"-and $_.ProcessId -ne {me} -and $_.ProcessId -ne {os.getppid()} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20, creationflags=0x08000000,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
    except Exception:
        pass


def _relaunch_detached() -> bool:
    """Started from a console? Re-start ourselves with pythonw, detached, so closing the terminal can't stop VAANI."""
    if not sys.platform.startswith("win") or os.environ.get("VAANI_CHILD") == "1":
        return False
    if "--foreground" in {a.lower() for a in sys.argv[1:]} or os.path.basename(sys.executable).lower().startswith("pythonw"):
        return False
    _kill_old_instances()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        return False
    try:
        import subprocess
        subprocess.Popen([str(pyw), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(Path(__file__).resolve().parent),
                         env={**os.environ, "VAANI_CHILD": "1"}, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200 | 0x08000000)   # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        return True
    except Exception:
        return False


def _run_app():
    _configure_logging()
    if not single_instance():
        logging.info("Another VAANI is already running - exiting this copy")
        return
    try:
        if not start_tray():
            logging.error("VAANI tray icon did not start. Check pystray and Pillow are installed.")
    except Exception:
        logging.exception("Tray icon startup failed")
    _warm_up_ollama()
    set_status("speaking", "Greeting")
    _say(greeting())                      # speak first; the speech models load right after
    set_status("starting", "Loading speech models")
    try:
        run_wake_mode()
    finally:
        request_shutdown()
        stop_tray()


if __name__ == "__main__":
    args = {arg.lower() for arg in sys.argv[1:]}
    if "--console" in args:
        _configure_logging()
        run_console()
    elif _relaunch_detached():
        print("VAANI is running in the background (microphone icon in the Windows notification area). You can close this window.\n"
              "Quit: right-click the VAANI tray icon > Exit VAANI.  Log: %LOCALAPPDATA%\\VAANI\\vaani.log")
    else:
        _run_app()