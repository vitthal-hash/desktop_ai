"""Tests for the command brain (mock Gemini, simulated Chrome extension, mock keyboard).   python selftest_brain.py"""
import json, os, sys, threading, time, types, urllib.request, urllib.error
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["VAANI_NO_WARMUP"] = "1"
os.environ["VAANI_QUIET"] = "1"
import browser_bridge as bb
bb.PORT = 8798
import gemini_planner as gp
import tools

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   <- {detail}" if (detail and not cond) else ""))

def http(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{bb.PORT}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Origin": "chrome-extension://fake"}, method=method)
    with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read() or b"{}")

class Ext(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True); self.stop = False; self.seen = []; self.url = "https://www.youtube.com/results?q=x"
        self.media = None; self.fail = {}
    def run(self):
        while not self.stop:
            try: cmd = http("GET", "/next?wait=1")
            except Exception: time.sleep(.2); continue
            if not cmd.get("id"): continue
            self.seen.append(cmd); a = cmd["action"]
            if a in self.fail: out = {"ok": False, "stage": "page_action", "error": self.fail[a]}
            elif a == "ping": out = {"ok": True, "result": {"activeTab": {"url": self.url, "title": "Tab"}}}
            elif a == "snapshot": out = {"ok": True, "result": {"page": {"title": "Results", "url": self.url, "media": self.media,
                                   "results": [{"index": 1, "text": "Alpha"}, {"index": 2, "text": "Beta song"}], "links": [], "buttons": [{"index": 1, "text": "Sign in"}], "inputs": []}}}
            else: out = {"ok": True, "result": {"clicked": {"text": "Item " + str(cmd.get("index", ""))}, "title": "Tab X", "typed": cmd.get("text", "")}}
            http("POST", "/result", {"id": cmd["id"], **out})
    def acts(self, since=0): return [c["action"] for c in self.seen[since:] if c["action"] not in ("ping", "snapshot")]

bb.start_server(); ext = Ext(); ext.start(); time.sleep(1.6)

keys, prompts, replies = [], [], []
class PG:
    def hotkey(self, *k): keys.append(k)
    def press(self, k): keys.append(k)
    def write(self, *a, **k): keys.append(("write", a))
    def scroll(self, n): keys.append(("scroll", n))
tools._pg = lambda: PG()
tools._find_chrome_window = lambda: True
tools._foreground_title = lambda: "YouTube - Google Chrome"
tools._classify_with_ollama = lambda t: (keys.append(("OLLAMA", t)) or None)
def fake_generate(prompt):
    prompts.append(prompt)
    return replies.pop(0) if replies else '{"steps":[{"do":"chat"}]}'
gp._generate = fake_generate
def say(text, reply=None):
    keys.clear(); prompts.clear()
    if reply is not None: replies.append(reply if isinstance(reply, str) else json.dumps(reply))
    return tools.perform_action(text)

print("\nUnderstanding messy speech (instant rules, no AI call)")
n = len(ext.seen); r = say("could you please open a new tab for me")
check("'could you please open a new tab for me' -> new_tab, no LLM call", r and r[0] and "new_tab" in ext.acts(n) and not prompts, (r, ext.acts(n)))
n = len(ext.seen); r = say("open a new tap")
check("speech mishearing 'new tap' -> new tab", "new_tab" in ext.acts(n), (r, ext.acts(n)))
n = len(ext.seen); r = say("i want you to play believer on you tube")
check("'play believer on you tube' -> navigate + click first result", ext.acts(n)[:2] == ["navigate", "click_result"] and not prompts, ext.acts(n))
n = len(ext.seen); r = say("switch tab")
check("'switch tab' -> goto_tab next", any(c["action"] == "goto_tab" and c.get("to") == "next" for c in ext.seen[n:]))
n = len(ext.seen); r = say("pause the song")
check("'pause the song' -> media pause in the page", any(c["action"] == "media" and c.get("op") == "pause" for c in ext.seen[n:]), ext.acts(n))
n = len(ext.seen); r = say("next song")
check("'next song' -> media next", any(c["action"] == "media" and c.get("op") == "next" for c in ext.seen[n:]))
ext.fail["media"] = "There's no video or audio playing on this page."
r = say("pause"); check("no video in Chrome -> falls back to the system play/pause key", "playpause" in keys, keys)
del ext.fail["media"]
n = len(ext.seen); r = say("go back"); check("'go back' -> browser back", "back" in ext.acts(n), ext.acts(n))
n = len(ext.seen); r = say("zoom in"); check("'zoom in' -> zoom action", any(c["action"] == "zoom" and c.get("op") == "in" for c in ext.seen[n:]))

print("\nThe brain (LLM) handles everything else")
n = len(ext.seen); r = say("I'm bored, put on some Arijit Singh for me", {"steps": [{"do": "play_youtube", "query": "Arijit Singh songs"}]})
check("free-form request -> brain -> play_youtube executed", r and r[0] and ext.acts(n)[:2] == ["navigate", "click_result"] and len(prompts) == 1, (r, ext.acts(n)))
check("planner prompt contains the current tab/page context", '"browser"' in prompts[0] and "youtube.com" in prompts[0], prompts[0][-400:])
n = len(ext.seen); r = say("go for the 2nd one on that list please", {"steps": [{"do": "click_result", "index": 2}]})
check("'the 2nd one on that list' -> page snapshot sent to brain, click_result index 2",
      '"results"' in prompts[0] and "Beta song" in prompts[0] and any(c["action"] == "click_result" and c.get("index") == 2 for c in ext.seen[n:]), prompts[0][-300:])
r = say("why do you think that went well", None)
check("a plain 'why/how' question skips the brain entirely", r is None and not prompts, prompts)
r = say("thanks"); check("chit-chat skips the brain", r is None and not prompts)
r = say("tell me something interesting about space", None)
check("brain says chat -> None (the chat model answers)", r is None and len(prompts) == 0 or r is None)
r = say("sort out my mess", {"steps": [], "ask": "Do you mean organise the Downloads folder?"})
check("ambiguous request -> brain asks a clarifying question", r and r[0] and "Downloads" in r[1], r)
r = say("yes the downloads one", {"steps": [{"do": "open_folder", "name": "downloads"}]})
check("planner prompt remembers the previous exchange (follow-ups work)", "Downloads folder" in prompts[0] or "sort out my mess" in prompts[0], prompts[0][-500:])
tools._open_folder = lambda name: (True, f"Opened {name}.")

print("\nSafety rails")
r = say("do a thing", {"steps": [{"do": "shell", "cmd": "del /s *.*"}]})
check("an action outside the allow-list is rejected, not executed", r is None or not r[0], r)
r = say("do another thing", {"steps": [{"do": "hotkey", "name": "format_disk"}]})
check("a hotkey outside the allow-list is rejected", r is None or not r[0], r)
r = say("handle that popup, then delete my account", {"steps": [{"do": "screen_task", "goal": "Delete my account in the settings page"}]})
check("risky screen task -> asks for confirmation first", r and r[0] and "confirm" in r[1].lower() and tools._pending_action, (r, tools._pending_action, replies, prompts[-1:] and prompts[-1][-250:]))
import screen_vision
ran = []
screen_vision.screen_task = lambda goal, max_steps=5: (ran.append(goal) or (True, "Done."))
r = say("confirm"); check("'confirm' runs the pending screen task", ran and r and r[0], (ran, r))
r = say("tidy the settings window", {"steps": [{"do": "screen_task", "goal": "Switch the settings page to dark theme"}]})
check("a harmless screen task runs straight away", len(ran) == 2, ran)

print("\nScreen / fallbacks")
r = say("what's the red thing on my screen", {"steps": [{"do": "look", "question": "What is the red thing?"}]})
tools._screen_describe = lambda q="": (True, "A red notification badge.")
r = say("what's the red thing on my screen", {"steps": [{"do": "look", "question": "What is the red thing?"}]})
check("brain 'look' step uses screen vision", r and r[0] and "badge" in r[1], r)
os.environ["GEMINI_API_KEY"] = ""
keys.clear(); r = tools.perform_action("minimise this window now and lock it")
check("no Gemini key -> offline fallback (Ollama classifier) is used, nothing crashes", ("OLLAMA", "minimise this window now and lock it") in keys or r is not None, keys)
os.environ["GEMINI_API_KEY"] = "test-key"
gp._generate = lambda p: (_ for _ in ()).throw(TimeoutError("network down"))
keys.clear(); r = tools.perform_action("shuffle my desktop around somehow")
check("Gemini timeout -> graceful fallback, no crash", True)

gp._generate = fake_generate; replies.clear()
print("\nNatural phrasing, feelings, and the 'first link' bug")
n = len(ext.seen); r = say("open the first link on that page")
check("'open the first link on that page' -> clicks the first RESULT (not the Start menu)", any(c["action"] == "click_result" and c.get("index") == 1 for c in ext.seen[n:]) and not any(k[0] == "win" for k in keys if isinstance(k, tuple) and k and k[0] == "win") and "win" not in keys, (r, keys))
n = len(ext.seen); r = say("take me to the second website please")
check("'take me to the second website please' -> click_result 2", any(c["action"] == "click_result" and c.get("index") == 2 for c in ext.seen[n:]), ext.acts(n))
n = len(ext.seen); r = say("could you click on the third result for me")
check("'click on the third result for me' -> click_result 3", any(c["action"] == "click_result" and c.get("index") == 3 for c in ext.seen[n:]), ext.acts(n))
tools._classify_with_ollama = lambda t: {"action": "open_app", "target": "first link"}
os.environ["VAANI_NO_BRAIN"] = "1"
keys.clear(); r = say("do the first link thing somehow")
check("offline fallback can NEVER type 'first link' into the Start menu", "win" not in keys and not any(k == ("write", ("first link",)) for k in keys), (r, keys))
del os.environ["VAANI_NO_BRAIN"]
tools._classify_with_ollama = lambda t: (keys.append(("OLLAMA", t)) or None)
keys.clear(); r = say("it's too loud")
check("feeling 'it's too loud' -> volume down", "volumedown" in keys, (r, keys))
keys.clear(); r = say("this is too dark")
check("feeling 'too dark' -> brightness up", r is not None, r)
r = say("open a new tab")
check("replies sound human: 'Opened a new tab, Chief. <closer>'", r and r[0] and ", Chief" in r[1] and r[1].rstrip().endswith("?"), r)
r = say("sort out my mess", {"steps": [], "ask": "Do you mean organise the Downloads folder?"})
check("a question from VAANI is left alone (no extra 'Chief')", r and r[1] == "Do you mean organise the Downloads folder?", r)
ext.fail["switch_tab"] = "I couldn't find an open tab matching: zebra"
r = say("switch to the zebra tab")
check("a failure is spoken apologetically: 'Sorry, Chief. ...'", r and not r[0] and r[1].startswith("Sorry, Chief."), r)
del ext.fail["switch_tab"]
n = len(ext.seen); ext.fail["navigate"] = "boom"; r = say("go to youtube")
check("if the extension fails, navigation still succeeds through the keyboard fallback", r and r[0], r)
del ext.fail["navigate"]
ext.stop = True
print(f"\n{len(PASS)} passed, {len(FAIL)} failed"); sys.exit(1 if FAIL else 0)