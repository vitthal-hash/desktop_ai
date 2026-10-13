"""VAANI self-test. Run from the project folder:   python selftest.py          (no Chrome / mic needed)
                                                    python selftest.py --live   (talks to the REAL extension)
Stage 1: bridge protocol with a simulated extension.  Stage 2: routing logic.  Live: real Chrome checks."""
import json, os, sys, threading, time, urllib.request, urllib.error
os.environ["VAANI_NO_BRAIN"] = "1"      # these tests check the instant rules only; the brain has its own tests
os.environ["VAANI_NO_WARMUP"] = "1"
os.environ["VAANI_QUIET"] = "1"

import browser_bridge as bb
if "--live" not in sys.argv:
    bb.PORT = 8799        # private port so a real Chrome extension can never answer the simulated tests

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   <- {detail}" if (detail and not cond) else ""))

def http(method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{bb.PORT}{path}", data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, {}

class FakeExtension(threading.Thread):
    """Behaves like background.js: long-polls /next and posts /result."""
    def __init__(self): super().__init__(daemon=True); self.stop = False; self.mode = "ok"; self.seen = []
    def run(self):
        while not self.stop:
            try: _, cmd = http("GET", "/next?wait=1", headers={"Origin": "chrome-extension://fake"})
            except Exception: time.sleep(0.2); continue
            if not cmd.get("id"): continue
            self.seen.append(cmd)
            if self.mode == "silent": continue                     # picks up but never answers
            if cmd["action"] == "ping":
                payload = {"ok": True, "result": {"alive": True, "activeTab": {"url": getattr(self, "url", "https://example.com/"), "title": "x"}}}
            elif cmd["action"] == "snapshot":
                payload = {"ok": True, "result": {"page": {"title": "T", "url": "https://example.com", "results": [{"index": 1, "text": "R1"}], "links": []}}}
            elif self.mode == "error":
                payload = {"ok": False, "stage": "page_action", "error": "boom"}
            else:
                payload = {"ok": True, "result": {"clicked": {"text": "Video " + str(cmd.get("index", ""))}, "navigated": cmd.get("url")}}
            http("POST", "/result", {"id": cmd["id"], **payload}, headers={"Origin": "chrome-extension://fake"})

def stage1():
    print("\nStage 1 - bridge protocol")
    bb.start_server(); time.sleep(0.3)
    t0 = time.time(); r = bb.command("snapshot", timeout=5)
    check("fails fast when extension not connected (<2s)", (not r["ok"]) and r.get("stage") == "not_connected" and time.time() - t0 < 2, r)
    ext = FakeExtension(); ext.start(); time.sleep(1.5)
    check("health reports extension connected", bb.browser_ready())
    r = bb.command("snapshot"); check("snapshot round trip", r["ok"] and r["result"]["page"]["title"] == "T", r)
    r1 = bb.command("click_result", index=2); r2 = bb.command("click_result", index=3)
    check("each command returns ITS OWN result (ids match)", r1["result"]["clicked"]["text"] == "Video 2" and r2["result"]["clicked"]["text"] == "Video 3", (r1, r2))
    ext.mode = "error"; r = bb.command("click_text", query="x")
    check("extension error is reported with its stage", (not r["ok"]) and r["stage"] == "page_action" and r["error"] == "boom", r)
    ext.mode = "silent"; t0 = time.time(); r = bb.command("click_text", query="x", timeout=2)
    check("silent extension -> 'no_result' within timeout", (not r["ok"]) and r["stage"] == "no_result" and time.time() - t0 < 5, r)
    ext.mode = "ok"
    code, _ = http("POST", "/command", {"action": "snapshot"}, headers={"Origin": "https://evil.example"})
    check("web page origin is rejected (403)", code == 403, code)
    code, _ = http("POST", "/command", {"action": "snapshot"}, headers={"Content-Type": "text/plain"})
    check("non-JSON content type rejected (415)", code == 415, code)
    ext.stop = True; time.sleep(1.2)
    bb._last_poll = time.monotonic() - 100                         # simulate extension gone
    check("stale heartbeat -> not connected", not bb.browser_ready())
    bb._last_poll = time.monotonic()
    return ext

def stage2():
    print("\nStage 2 - routing (mocked keyboard + models)")
    import tools
    calls = []
    class PG:
        def hotkey(self, *k): calls.append(("hotkey", k))
        def press(self, k): calls.append(("press", k))
        def write(self, *a, **k): calls.append(("write", a))
        def scroll(self, n): calls.append(("scroll", n))
    tools._pg = lambda: PG()
    tools._find_chrome_window = lambda: True
    tools._classify_with_ollama = lambda t: (calls.append(("OLLAMA", t)) or None)
    tools._bridge_ready = lambda: False                            # extension off
    def run(text): calls.clear(); return tools.perform_action(text)

    r = run("Calculate 2 plus 2.");            check("math -> 4", r and "4" in r[1], r)
    r = run("what is 12 times 3");             check("'what is 12 times 3' -> 36", r and "36" in r[1], r)
    r = run("what is the capital of 5 countries"); check("non-math 'what is...' is NOT calculated", r is None, r)
    r = run("scroll up");                      check("scroll up", r and r[0] and ("scroll", 5) in calls, (r, calls))
    r = run("open a new tab");                 check("new tab works with extension OFF (keyboard fallback)", r and r[0] and ("hotkey", ("ctrl", "t")) in calls, (r, calls))
    r = run("what is photosynthesis");         check("plain question skips classifier -> chat", r is None and not any(c[0] == "OLLAMA" for c in calls), calls)
    r = run("open the second video");          check("page command with extension off gives a clear error, no hang", r and not r[0] and "isn't connected" in r[1], r)
    r = run("send a message to Sam saying hello there")
    check("WhatsApp asks for confirmation first", r and r[0] and "confirm" in r[1].lower() and not any(c[0] == "write" for c in calls), (r, calls))
    r = run("cancel");                         check("cancel clears pending send", r and "Cancelled" in r[1], r)
    r = run("what time is it");                check("time answered locally", r and r[0] and ":" in r[1], r)
    r = run("go to the third tab");            check("go to the third tab -> Ctrl+3", ("hotkey", ("ctrl", "3")) in calls, (r, calls))

    print("   (now with simulated extension ON)")
    ext = FakeExtension(); ext.start(); time.sleep(1.5)
    tools._bridge_ready = bb.browser_ready
    r = run("open the second video");          check("'open the second video' -> click_result via bridge", r and r[0] and "Video 2" in r[1], r)
    n = len(ext.seen); r = run("open youtube")
    check("'open youtube' navigates via the extension (not keyboard)", r and r[0] and any(c["action"] == "navigate" for c in ext.seen[n:]) and not calls, (r, calls))
    n = len(ext.seen); r = run("play python tutorial on youtube")
    check("'play X on youtube' = navigate then click first result", r and r[0] and [c["action"] for c in ext.seen[n:]][:2] == ["navigate", "click_result"], (r, [c["action"] for c in ext.seen[n:]]))
    r = run("read this page"); check("'read this page' uses snapshot", r and r[0] and "You're on T" in r[1], r)

    # ---- context, tabs, chaining, screen vision ----
    r = run("search for youtube")
    check("'search for youtube' just opens YouTube", r and r[0] and any(c["action"] == "navigate" and c["url"].rstrip("/") == "https://www.youtube.com" for c in ext.seen), r)
    ext.url = "https://www.youtube.com/"
    n = len(ext.seen); r = run("search for lofi music")
    nav = [c for c in ext.seen[n:] if c["action"] == "navigate"]
    check("while ON YouTube, 'search for X' searches YouTube (no 'on Chrome' needed)", nav and "youtube.com/results" in nav[0]["url"], (r, ext.seen[n:]))
    ext.url = "https://example.com/"
    n = len(ext.seen); r = run("switch tab")
    check("'switch tab' -> next tab via Chrome API", any(c["action"] == "goto_tab" and c.get("to") == "next" for c in ext.seen[n:]), (r, ext.seen[n:]))
    n = len(ext.seen); r = run("go to the last tab")
    check("'last tab' -> goto_tab last", any(c["action"] == "goto_tab" and c.get("to") == "last" for c in ext.seen[n:]), (r, ext.seen[n:]))
    n = len(ext.seen); r = run("go to the fifth tab")
    check("'fifth tab' -> goto_tab index 5", any(c["action"] == "goto_tab" and c.get("index") == 5 for c in ext.seen[n:]), (r, ext.seen[n:]))
    n = len(ext.seen); r = run("open a new tab")
    check("'open a new tab' -> extension new_tab", any(c["action"] == "new_tab" for c in ext.seen[n:]), (r, ext.seen[n:]))

    import screen_vision, types, sys as _sys
    clicked = []
    _sys.modules["pyautogui"] = types.SimpleNamespace(click=lambda x, y, clicks=1: clicked.append((x, y)))
    screen_vision._capture = lambda *a, **k: (b"png", (2000, 1000))
    boxes = [("Zed", [100, 600, 400, 800]), ("Amy", [100, 100, 400, 300]), ("Bob", [100, 350, 400, 550]), ("Cat", [500, 100, 800, 300])]  # scrambled order
    state = {"calls": 0}
    def fake_ask(prompt, png, want_json=True):
        state["calls"] += 1
        if "profile picker" in prompt:
            if state["calls"] == 1: return {"is_profile_picker": True, "profiles": [{"name": n_, "box_2d": b} for n_, b in boxes]}, None
            return {"is_profile_picker": False, "profiles": []}, None
        if "Find this on the screen" in prompt: return {"found": True, "label": "Sign in", "box_2d": [0, 0, 100, 100]}, None
        return "A web page about cats.", None
    screen_vision._ask = fake_ask
    state["calls"] = 0; clicked.clear(); r = run("select the third profile")
    check("'select the third profile' clicks the 3rd card in reading order (Zed, after sorting Amy/Bob/Zed)", r and r[0] and clicked == [(1400, 250)] and "Zed" in r[1], (r, clicked))
    state["calls"] = 0; clicked.clear(); r = run("open the fifth profile")
    check("profile number too high -> clear message", r and not r[0] and "4 profiles" in r[1], r)
    state["calls"] = 0; clicked.clear(); r = run("open Cat profile")
    check("profile by name", r and r[0] and clicked == [(400, 650)] and "Cat" in r[1], (r, clicked))
    state["calls"] = 0; clicked.clear(); t0 = time.time(); r = run("open chrome and select the third profile")
    check("chained: 'open chrome and select the third profile'", r and r[0] and clicked == [(1400, 250)], (r, clicked))
    tools._bridge_ready = lambda: False
    r = run("what's on my screen"); check("'what's on my screen' uses screen vision", r and r[0] and "cats" in r[1], r)
    clicked.clear(); r = run("click the sign in button"); check("click falls back to screen vision when DOM/bridge unavailable", r and r[0] and clicked, (r, clicked))
    r = run("search for salt and pepper"); check("'and' inside a query is NOT split into steps", not (r and "didn't understand" in r[1]), r)
    tools._bridge_ready = bb.browser_ready
    ext.stop = True

def live():
    print("\nLIVE - real Chrome extension")
    bb.start_server(); print("Waiting up to 20s for the extension to connect..."); 
    for _ in range(40):
        if bb.browser_ready(): break
        time.sleep(0.5)
    check("extension connected", bb.browser_ready(), "Open Chrome; reload the extension at chrome://extensions")
    if not bb.browser_ready(): return
    r = bb.command("ping"); check("ping returns active tab", r["ok"], r); print("      ", r)
    r = bb.command("snapshot"); check("snapshot of the active tab", r["ok"], r)
    if r["ok"]:
        p = r["result"]["page"]; print("       title:", p["title"], "| url:", p["url"], "| links:", len(p["links"]), "| results:", len(p["results"]))
    r = bb.command("navigate", url="https://www.youtube.com/results?search_query=python+tutorial"); check("navigate to YouTube search", r["ok"], r); print("      ", r)
    time.sleep(3); r = bb.command("snapshot"); check("YouTube results detected", r["ok"] and len(r["result"]["page"]["results"]) > 0, r)

if __name__ == "__main__":
    if "--live" in sys.argv: live()
    else: ext = stage1(); stage2()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed"); sys.exit(1 if FAIL else 0)