"""Is Gemini connected?   python brain_check.py      (run in the VAANI folder, venv active)
Checks: .env found -> key present -> planner call works (and how fast) -> screen-vision call works."""
import io, os, sys, time

import env_loader

print("\n=== VAANI Gemini check ===\n")
loaded = env_loader.load_env()
key = os.getenv("GEMINI_API_KEY", "").strip()
env_file = os.path.join(os.path.dirname(os.path.abspath(env_loader.__file__)), ".env")
print(f"1. .env file at {env_file}: {'FOUND' if os.path.isfile(env_file) else 'NOT FOUND  <- create it there with a line:  GEMINI_API_KEY=your-key'}")
if not key:
    print("2. GEMINI_API_KEY: MISSING. Put   GEMINI_API_KEY=your-key   in that .env file (no spaces, no quotes needed).")
    sys.exit(1)
print(f"2. GEMINI_API_KEY: present ({len(key)} characters, starts with {key[:4]}...)." + ("" if len(key) >= 30 else "  <- that looks too short; check you copied the whole key."))


def explain(err: str) -> str:
    e = err.lower()
    if "deadline" in e and "too short" in e:
        return "A timeout setting below Google's 10 second minimum. Update gemini_planner.py to the latest version."
    if "api key not valid" in e or "api_key_invalid" in e:
        return "Google says the key is NOT VALID. Create a new key at https://aistudio.google.com/apikey and replace it in .env (no extra spaces/quotes)."
    if "permission" in e or "403" in e:
        return "The key is valid but not allowed to use this model/API. Check the key's restrictions in Google AI Studio."
    if "503" in e or "unavailable" in e or "high demand" in e:
        return "Google's model is temporarily busy. Try again in a minute; VAANI automatically retries and falls back."
    if "429" in e or "quota" in e or "resource_exhausted" in e:
        return "Quota/rate limit reached. Wait a minute, or check your plan and quota in Google AI Studio."
    if "404" in e or "not found" in e:
        return "That model name isn't available to this key. Set GEMINI_MODEL=gemini-2.5-flash-lite (or another current model) in .env."
    if "timeout" in e or "connect" in e or "getaddrinfo" in e or "network" in e:
        return "Network problem: this PC couldn't reach Google. Check the internet, VPN or firewall."
    return "See the error above."


print("\n3. Planner (the brain that understands your sentences)")
import gemini_planner as gp
print(f"   model: {gp.MODEL}")
t0 = time.time()
steps, ask, err = gp.plan_steps("open a new tab and search for best laptops", {"browser": {"connected": True}})
ms = int((time.time() - t0) * 1000)
if err:
    print(f"   FAILED after {ms} ms: {err}\n   -> {explain(err)}")
    brain_ok = False
else:
    print(f"   OK in {ms} ms. It planned: {steps}")
    brain_ok = True
    t0 = time.time(); gp.plan_steps("pause", {"browser": {"connected": True, "media": {"playing": True}}})
    print(f"   second call (warm connection): {int((time.time() - t0) * 1000)} ms   <- this is the delay you feel on non-instant commands")

print("\n4. Screen vision (for 'what's on my screen', clicking things in any app)")
try:
    from PIL import Image
    import screen_vision
    buf = io.BytesIO(); Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "PNG")
    data, verr = screen_vision._ask('This image is one solid colour. Return JSON: {"colour": "<name>"}', buf.getvalue())
    print(f"   OK: {data}" if not verr else f"   FAILED: {verr}\n   -> {explain(verr)}")
except Exception as exc:
    print(f"   FAILED: {type(exc).__name__}: {exc}")

print("\n=== RESULT ===")
print("Gemini brain is CONNECTED." if brain_ok else "Gemini brain is NOT working - fix the item marked FAILED above, then run this again.")