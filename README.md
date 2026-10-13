# VAANI — consistent Windows tray bundle

This bundle aligns the app, control module, tray module, action layer, speech modules, browser bridge, and Chrome extension into one source set. It uses a **normal Windows notification-area microphone icon**, not a floating orb; status colors are steady and do not pulse.

## Included behavior
- Wake phrase: **“Hey Computer”**. The local Faster-Whisper model handles wake/command transcription.
- Local conversation: Ollama model `llama3.2:3b`.
- Windows actions and browser actions through the Python action layer.
- Optional Gemini planning/vision features when `GEMINI_API_KEY` is configured.
- Tray menu: pause/resume microphone, show status, exit.
- Chrome extension under `chrome_extension/` communicates with the local bridge on `127.0.0.1:8765`.
- Logs: `%LOCALAPPDATA%\VAANI\vaani.log`.

## Install without replacing your existing environment
1. Stop any running VAANI instance. Extract this folder.
2. If you are updating an existing project, **back up that folder first**. Copy these source files together as a set; do not mix this bundle with older `main.py`, `vaani_control.py`, or `tray_status.py`. Keep your existing `.venv` if it is already at the project root.
3. Open PowerShell in this folder and install/update dependencies in the existing environment:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Confirm Ollama is running and `llama3.2:3b` exists:

   ```powershell
   ollama list
   ```

5. Load the extension: open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**, and select this bundle's `chrome_extension` folder. Pin VAANI Browser Bridge if desired.
6. Run `start_vaani.bat` or `.\.venv\Scripts\python.exe main.py --console` (foreground diagnostic mode).

## First start notes
- Faster-Whisper may need to download model files on its first run.
- Voice enrollment is optional; `speaker_lock.py` stores enrollment data locally. Run `enroll_voice.py` only if you want to set it up.
- Gemini is optional. Without `GEMINI_API_KEY`, browser commands should use deterministic routes where available; planner/vision features may be unavailable. Never put your key into the Chrome extension.
- Windows security software, audio devices, browser permissions, and installed apps affect runtime behavior. No code bundle can guarantee every external environment behaves identically.

## Checks performed for this package
Python files were syntax-compiled and module references were checked for consistency. This environment cannot test your actual Windows microphone, Ollama daemon, Chrome profile/extension runtime, or local API credentials; please run the smoke tests below on your laptop.

## Smoke test (one at a time)
1. Start VAANI and confirm the tray mic icon appears.
2. Right-click it → **Show status**; pause then resume microphone.
3. Say “Hey Computer”, then ask a simple question.
4. Try `calculate 2 plus 2`.
5. In Chrome, test `open a new tab`, then `search YouTube for Python tutorials`. Confirm the current clipboard contents remain unchanged.
6. If a browser command fails, check `%LOCALAPPDATA%\VAANI\vaani.log` and the extension's service-worker console before repeating it.
