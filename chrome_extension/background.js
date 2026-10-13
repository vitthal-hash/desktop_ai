// VAANI Browser Bridge v3 - service worker.
// Fixes: MV3 workers are killed after ~30 s idle, so setInterval polling silently died.
// Now: long-poll loop (each response resets the idle timer) + chrome.alarms to revive the worker.
const API = "http://127.0.0.1:8765";
const INTERNAL = /^(chrome|edge|about|devtools|chrome-extension|view-source):/i;
let loopRunning = false;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function post(path, data) {
  try {
    const r = await fetch(API + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data), cache: "no-store"
    });
    return r.ok;
  } catch (e) { console.error("VAANI POST failed", path, e); return false; }
}

class StageError extends Error {
  constructor(stage, message) { super(message); this.stage = stage; }
}

async function getActiveTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0] && Number.isInteger(tabs[0].id) && !INTERNAL.test(tabs[0].url || "")) return tabs[0];
  tabs = await chrome.tabs.query({ active: true, windowType: "normal" });
  const normal = tabs.filter(t => Number.isInteger(t.id) && !INTERNAL.test(t.url || ""));
  if (normal.length) return normal[0];
  // Allow an internal/new-tab page for navigate-type commands (caller decides).
  return tabs[0] || null;
}

// Resolves true when the tab reports status "complete" (after a navigation starts), false on timeout.
// Register BEFORE triggering navigation (tabs.update) so the event can't be missed.
function waitForLoad(tabId, timeoutMs = 10000) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (done) return; done = true; chrome.tabs.onUpdated.removeListener(l); clearTimeout(t); resolve(v); };
    const l = (id, info) => { if (id === tabId && info.status === "complete") finish(true); };
    const t = setTimeout(() => finish(false), timeoutMs);
    chrome.tabs.onUpdated.addListener(l);
  });
}

async function sendToTab(tab, cmd) {
  if (INTERNAL.test(tab.url || "")) {
    throw new StageError("internal_page", "The active tab is a Chrome internal page. Open a normal website and try again.");
  }
  const msg = { type: "VAANI_ACTION", command: cmd };
  try {
    return await chrome.tabs.sendMessage(tab.id, msg);
  } catch (first) {
    try {   // content script missing (tab opened before install / not yet loaded) -> inject, retry
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      await sleep(250);
      return await chrome.tabs.sendMessage(tab.id, msg);
    } catch (second) {
      throw new StageError("page_messaging",
        "Couldn't talk to the page. Refresh the tab and try again. (" + String(second.message || first.message) + ")");
    }
  }
}

async function handle(cmd) {
  const a = cmd.action;
  if (a === "ping") {
    const tab = await getActiveTab();
    return { alive: true, activeTab: tab ? { id: tab.id, title: tab.title, url: tab.url } : null };
  }
  if (a === "list_tabs") {
    const tabs = await chrome.tabs.query({});
    return tabs.map((t, i) => ({ index: i + 1, id: t.id, title: t.title || "", url: t.url || "", active: !!t.active }));
  }
  if (a === "switch_tab") {
    const q = String(cmd.query || "").trim().toLowerCase();
    if (!q) throw new StageError("bad_command", "No tab name given.");
    const words = q.split(/[^a-z0-9]+/).filter(w => w.length > 1 && !["tab", "the", "page", "window", "my", "to", "of", "on"].includes(w));
    const tabs = await chrome.tabs.query({});
    const scored = tabs.map(t => {
      const hay = ((t.title || "") + " " + (t.url || "")).toLowerCase();
      let s = 0;
      for (const w of words) if (hay.includes(w)) s += 2 + (hay.includes(" " + w) ? 1 : 0);
      if (hay.includes(q)) s += 4;
      if (t.active) s -= 0.5;                       // prefer a different tab than the current one on ties
      return { t, s };
    }).filter(x => x.s > 0).sort((x, y) => y.s - x.s);
    if (!scored.length) throw new StageError("tab_not_found", "I couldn't find an open tab matching: " + q);
    const t = scored[0].t;
    await chrome.tabs.update(t.id, { active: true });
    await chrome.windows.update(t.windowId, { focused: true });
    return { title: t.title || "", url: t.url || "", matchCount: scored.length };
  }
  if (a === "goto_tab") {
    const tabs = (await chrome.tabs.query({ lastFocusedWindow: true })).sort((x, y) => x.index - y.index);
    if (!tabs.length) throw new StageError("no_tab", "No tabs found.");
    const cur = Math.max(0, tabs.findIndex(t => t.active));
    let i;
    if (cmd.to === "next") i = (cur + 1) % tabs.length;
    else if (cmd.to === "prev") i = (cur - 1 + tabs.length) % tabs.length;
    else if (cmd.to === "last") i = tabs.length - 1;
    else if (cmd.to === "first") i = 0;
    else i = Number(cmd.index || 1) - 1;
    if (i < 0 || i >= tabs.length) throw new StageError("tab_not_found", `There are only ${tabs.length} tabs open.`);
    await chrome.tabs.update(tabs[i].id, { active: true });
    return { title: tabs[i].title || "", url: tabs[i].url || "", index: i + 1, total: tabs.length };
  }
  if (["back", "forward", "reload", "zoom", "duplicate_tab", "mute_tab", "pin_tab"].includes(a)) {
    const tab = await getActiveTab();
    if (!tab) throw new StageError("no_tab", "No active browser tab.");
    if (a === "back") { await chrome.tabs.goBack(tab.id); return { went: "back" }; }
    if (a === "forward") { await chrome.tabs.goForward(tab.id); return { went: "forward" }; }
    if (a === "reload") { await chrome.tabs.reload(tab.id); return { reloaded: true }; }
    if (a === "duplicate_tab") { await chrome.tabs.duplicate(tab.id); return { duplicated: true }; }
    if (a === "mute_tab") { const muted = !(tab.mutedInfo && tab.mutedInfo.muted); await chrome.tabs.update(tab.id, { muted }); return { muted }; }
    if (a === "pin_tab") { await chrome.tabs.update(tab.id, { pinned: !tab.pinned }); return { pinned: !tab.pinned }; }
    if (a === "zoom") {
      const cur = await chrome.tabs.getZoom(tab.id);
      const next = cmd.op === "reset" ? 1 : Math.min(5, Math.max(0.25, cmd.op === "out" ? cur - 0.1 : cur + 0.1));
      await chrome.tabs.setZoom(tab.id, next);
      return { zoom: Math.round(next * 100) };
    }
  }
  if (a === "new_window") {
    const w = await chrome.windows.create({ url: cmd.url || undefined, focused: true });
    return { windowId: w.id };
  }
  if (a === "new_tab") {
    const t = await chrome.tabs.create({ url: cmd.url || undefined, active: true });
    if (cmd.url) await waitForLoad(t.id);
    return { tabId: t.id, opened: cmd.url || "new tab" };
  }
  if (a === "close_tab") {
    const tab = await getActiveTab();
    if (!tab) throw new StageError("no_tab", "No active tab to close.");
    await chrome.tabs.remove(tab.id);
    return { closed: tab.title || tab.url };
  }
  if (a === "navigate") {
    const url = String(cmd.url || "");
    if (!/^https?:\/\//i.test(url)) throw new StageError("bad_command", "Refusing to navigate to a non-http(s) address.");
    let tab = await getActiveTab();
    let loaded;
    if (cmd.new_tab || !tab) {
      tab = await chrome.tabs.create({ url, active: true });
      loaded = await waitForLoad(tab.id, 10000);
    } else {
      const waiting = waitForLoad(tab.id, 10000);      // listener first, then navigate
      await chrome.tabs.update(tab.id, { url, active: true });
      loaded = await waiting;
    }
    const fresh = await chrome.tabs.get(tab.id);
    return { navigated: url, tabId: tab.id, loaded, title: fresh.title || "", url: fresh.url || url };
  }
  // Everything else is a DOM action performed by the content script in the active tab.
  const tab = await getActiveTab();
  if (!tab) throw new StageError("no_tab", "No active browser tab. Open a normal Chrome website first.");
  const result = await sendToTab(tab, cmd);
  if (result === undefined) throw new StageError("page_no_reply", "The page didn't answer. Refresh the tab and try again.");
  if (result && result.error) throw new StageError("page_action", result.error);
  return result;
}

async function loop() {
  if (loopRunning) return;
  loopRunning = true;
  try {
    while (true) {
      let cmd = null;
      try {
        const r = await fetch(API + "/next?wait=15", { cache: "no-store" });
        if (!r.ok) { await sleep(2000); continue; }
        cmd = await r.json();
      } catch (e) {            // VAANI not running yet -> retry quietly
        await sleep(2000);
        continue;
      }
      if (!cmd || !cmd.id) continue;
      try {
        const result = await handle(cmd);
        await post("/result", { id: cmd.id, ok: true, result });
      } catch (e) {
        console.error("VAANI command failed:", cmd.action, e);
        await post("/result", { id: cmd.id, ok: false, stage: e.stage || "extension_error", error: String(e.message || e) });
      }
    }
  } finally { loopRunning = false; }
}

// Keep the worker alive / revive it if Chrome killed it.
chrome.alarms.create("vaani-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { loop(); });
chrome.runtime.onStartup.addListener(() => { loop(); });
chrome.runtime.onInstalled.addListener(() => { loop(); });
loop();