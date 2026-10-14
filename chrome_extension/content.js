// VAANI Browser Bridge v2 - content script (DOM-backed actions).
// Guard: the script is loaded by the manifest AND may be re-injected by background.js.
// Without this guard every action ran twice (double clicks).
(() => {
  if (window.__vaaniContentLoaded) return;
  window.__vaaniContentLoaded = true;

  const clean = v => (v || "").replace(/\s+/g, " ").trim();

  // The element that actually scrolls on this page (the biggest scrollable area under the screen centre / left-centre).
  function findScroller() {
    const root = document.scrollingElement || document.documentElement;
    const canScroll = n => {
      if (!n || n === document.body || n === document.documentElement) return false;
      const s = getComputedStyle(n);
      return /(auto|scroll|overlay)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 10;
    };
    if (root.scrollHeight > root.clientHeight + 10 && !/(hidden)/.test(getComputedStyle(document.body).overflowY)) {
      return root;                                    // an ordinary page: the page itself scrolls
    }
    const pts = [[0.5, 0.5], [0.35, 0.5], [0.65, 0.5], [0.5, 0.3], [0.5, 0.75]];
    for (const [fx, fy] of pts) {
      for (let n = document.elementFromPoint(innerWidth * fx, innerHeight * fy); n; n = n.parentElement) {
        if (canScroll(n)) return n;
      }
    }
    let best = null, bestArea = 0;                    // last resort: the largest scrollable element
    for (const n of document.querySelectorAll("div, main, section, ul, ol")) {
      if (!canScroll(n)) continue;
      const r = n.getBoundingClientRect();
      const area = r.width * r.height;
      if (area > bestArea) { best = n; bestArea = area; }
    }
    return best || root;
  }

  // ---- YouTube: skip ads automatically, so Chief never has to say "skip the ad" ----
  const FAST_FORWARD_UNSKIPPABLE_ADS = true;          // set to false to only ever click "Skip"
  if (/(^|\.)youtube\.com$/.test(location.hostname)) {
    let sped = false, prev = { muted: false, rate: 1 };
    setInterval(() => {
      try {
        const player = document.querySelector("#movie_player");
        const video = document.querySelector("video.html5-main-video") || document.querySelector("video");
        const adShowing = !!(player && player.classList.contains("ad-showing"));
        const skip = document.querySelector(
          ".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button-container button");
        if (skip && skip.offsetParent !== null) skip.click();
        const overlayClose = document.querySelector(".ytp-ad-overlay-close-button");
        if (overlayClose) overlayClose.click();
        if (FAST_FORWARD_UNSKIPPABLE_ADS && adShowing && video) {
          if (!sped) { prev = { muted: video.muted, rate: video.playbackRate || 1 }; sped = true; }
          video.muted = true;
          video.playbackRate = 16;
        } else if (sped && video) {
          video.muted = prev.muted;
          video.playbackRate = prev.rate;
          sped = false;
        }
      } catch (e) { /* never break the page */ }
    }, 400);
  }

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  }
  function labelOf(el) {
    return clean(el.innerText || el.getAttribute("aria-label") || el.title || el.alt || el.value || el.placeholder || "");
  }

  // ---- site-aware "search results" (what a user means by "the second video / website") ----
  function getResults() {
    const host = location.hostname;
    let items = [];
    if (/youtube\.com$/.test(host) || host.endsWith(".youtube.com")) {
      items = [...document.querySelectorAll("ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title-link, ytd-compact-video-renderer a.yt-simple-endpoint, a#video-title")]
        .filter(isVisible)
        .map(a => ({ el: a, text: clean(a.getAttribute("title") || a.innerText || a.getAttribute("aria-label")), href: a.href }));
    } else if (/google\./.test(host)) {
      items = [...document.querySelectorAll("#search a:has(h3), #rso a:has(h3)")]
        .filter(isVisible)
        .map(a => ({ el: a, text: clean(a.querySelector("h3")?.innerText), href: a.href }));
    } else if (/bing\.com$/.test(host)) {
      items = [...document.querySelectorAll("li.b_algo h2 a")].filter(isVisible)
        .map(a => ({ el: a, text: clean(a.innerText), href: a.href }));
    } else if (/duckduckgo\.com$/.test(host)) {
      items = [...document.querySelectorAll("a[data-testid='result-title-a']")].filter(isVisible)
        .map(a => ({ el: a, text: clean(a.innerText), href: a.href }));
    }
    if (!items.length) {          // unknown site / changed layout: any visible link wrapping a heading counts as a result
      items = [...document.querySelectorAll("a:has(h3), a:has(h2)")].filter(isVisible)
        .map(a => ({ el: a, text: clean((a.querySelector("h3, h2"))?.innerText), href: a.href }))
        .filter(i => i.text);
    }
    const seen = new Set();
    return items.filter(i => i.href && !seen.has(i.href) && seen.add(i.href));
  }

  function getSnapshot() {
    const links = [...document.querySelectorAll("a[href]")].filter(isVisible).slice(0, 80)
      .map((a, i) => ({ index: i + 1, text: labelOf(a), href: a.href }))
      .filter(x => x.text || x.href);
    const buttons = [...document.querySelectorAll("button, [role=button], input[type=submit], input[type=button]")]
      .filter(isVisible).slice(0, 60)
      .map((b, i) => ({ index: i + 1, text: clean(labelOf(b) || b.value) })).filter(x => x.text);
    const inputs = [...document.querySelectorAll("input:not([type=hidden]), textarea, [contenteditable=true], [role=textbox]")]
      .filter(isVisible).slice(0, 40)
      .map((e, i) => ({ index: i + 1, tag: e.tagName.toLowerCase(), type: e.type || "", placeholder: e.placeholder || "",
                        aria: e.getAttribute("aria-label") || "", name: e.name || "" }));
    const v = document.querySelector("video, audio");
    const media = v ? { playing: !v.paused && !v.ended, muted: v.muted, current: Math.round(v.currentTime), duration: Math.round(v.duration || 0), rate: v.playbackRate } : null;
    const results = getResults().slice(0, 20).map((r, i) => ({ index: i + 1, text: r.text, href: r.href }));
    return { title: document.title, url: location.href, text: clean(document.body?.innerText).slice(0, 12000),
             results, links, buttons, inputs, media };
  }

  function findTextBox(hint) {
    const q = (hint || "").toLowerCase();
    const els = [...document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea, [contenteditable=true], [role=textbox]")]
      .filter(isVisible);
    const score = e => {
      const meta = [e.placeholder, e.getAttribute("aria-label"), e.name, e.id, e.getAttribute("data-testid"), e.getAttribute("role")].join(" ").toLowerCase();
      let v = 0;
      if (q && meta.includes(q)) v += 20;
      if (e.type === "search" || /search/.test(meta)) v += 8;
      if (e.tagName === "TEXTAREA") v += 3;
      if (e.isContentEditable) v += 3;
      return v;
    };
    return els.sort((a, b) => score(b) - score(a))[0] || null;
  }

  function setValue(box, value) {
    box.focus();
    if (box.isContentEditable) {
      document.execCommand("selectAll", false);
      document.execCommand("insertText", false, value);
      if (clean(box.textContent) !== clean(value)) box.textContent = value;
    } else {
      const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      setter ? setter.call(box, value) : (box.value = value);
    }
    box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function submitFrom(box) {
    // 1) real form submit  2) a nearby search/submit button  3) Enter key events
    const form = box.closest("form");
    if (form) { try { form.requestSubmit(); return "form"; } catch (e) {} }
    const scope = box.closest("form, header, [role=search], div") || document;
    const btn = [...scope.querySelectorAll("button, [role=button], input[type=submit]")]
      .filter(isVisible).find(b => /search|go|send|submit/i.test(labelOf(b) + " " + (b.id || "")));
    if (btn) { btn.click(); return "button"; }
    for (const type of ["keydown", "keypress", "keyup"]) {
      box.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    }
    return "enter-key";
  }

  function clickEl(el) {
    el.scrollIntoView({ block: "center", inline: "nearest" });
    el.click();
  }

  function bestMatch(elements, query) {
    const q = query.toLowerCase();
    const scored = elements.map(el => {
      const t = labelOf(el).toLowerCase();
      let s = 0;
      if (t === q) s = 100; else if (t.startsWith(q)) s = 60; else if (t.includes(q)) s = 40 - Math.min(30, t.length / 20);
      return { el, s };
    }).filter(x => x.s > 0).sort((a, b) => b.s - a.s);
    return scored[0]?.el || null;
  }

  function perform(cmd) {
    switch (cmd.action) {
      case "snapshot":
        return { page: getSnapshot() };

      case "click_result": {
        const results = getResults();
        const i = Math.max(1, Number(cmd.index || 1)) - 1;
        if (!results.length) throw new Error("I can't find search results on this page. Try 'click link number N' instead.");
        const r = results[i];
        if (!r) throw new Error(`There are only ${results.length} results on this page.`);
        clickEl(r.el);
        return { clicked: { index: i + 1, text: r.text, href: r.href } };
      }

      case "click_link": {
        const links = [...document.querySelectorAll("a[href]")].filter(isVisible).slice(0, 80);
        const i = Math.max(1, Number(cmd.index || 1)) - 1;
        const link = links[i];
        if (!link) throw new Error("That visible link number does not exist.");
        const details = { text: labelOf(link), href: link.href };
        clickEl(link);
        return { clicked: details };
      }

      case "click_text": {
        const q = clean(cmd.query);
        if (!q) throw new Error("No text to look for.");
        const els = [...document.querySelectorAll("a, button, [role=button], [role=link], [role=tab], [role=menuitem], [role=option], [role=checkbox], summary, label, input[type=submit], input[type=button], [onclick], ytd-video-renderer, ytd-rich-item-renderer")].filter(isVisible);
        const el = bestMatch(els, q);
        if (!el) throw new Error("Could not find a visible element containing: " + q);
        clickEl(el);
        return { clicked: labelOf(el).slice(0, 120) };
      }

      case "click_button": {
        const q = clean(cmd.query);
        if (!q) throw new Error("No button name given.");
        const els = [...document.querySelectorAll("button, [role=button], input[type=submit], input[type=button], a")].filter(isVisible);
        const el = bestMatch(els, q);
        if (!el) throw new Error("Couldn't find a visible button or link named " + q);
        clickEl(el);
        return { clicked: (labelOf(el) || el.value || "").slice(0, 120) };
      }

      case "search":
      case "type": {
        const box = findTextBox(cmd.hint || "search");
        if (!box) throw new Error("Couldn't identify a visible text box on this page.");
        const value = String(cmd.query || cmd.text || "");
        setValue(box, value);
        let how = null;
        if (cmd.submit) how = submitFrom(box);
        return { typed: value, submitted: !!cmd.submit, how, box: box.placeholder || box.getAttribute("aria-label") || box.tagName };
      }

      case "media": {
        const v = document.querySelector("video") || document.querySelector("audio");
        const yt = /(^|\.)youtube\.com$/.test(location.hostname);
        const op = cmd.op || "toggle";
        const click = sel => { const b = document.querySelector(sel); if (b) { b.click(); return true; } return false; };
        if (!v && !["next", "previous"].includes(op)) throw new Error("There's no video or audio playing on this page.");
        switch (op) {
          case "play": v.play(); return { media: "playing" };
          case "pause": v.pause(); return { media: "paused" };
          case "toggle": if (v.paused) { v.play(); return { media: "playing" }; } v.pause(); return { media: "paused" };
          case "mute": v.muted = true; return { media: "muted" };
          case "unmute": v.muted = false; return { media: "unmuted" };
          case "skip_ahead": v.currentTime = Math.min((v.duration || 1e9), v.currentTime + Number(cmd.seconds || 10)); return { media: "skipped ahead" };
          case "skip_back": v.currentTime = Math.max(0, v.currentTime - Number(cmd.seconds || 10)); return { media: "skipped back" };
          case "faster": v.playbackRate = Math.min(2, Math.round((v.playbackRate + 0.25) * 100) / 100); return { media: "speed " + v.playbackRate };
          case "slower": v.playbackRate = Math.max(0.25, Math.round((v.playbackRate - 0.25) * 100) / 100); return { media: "speed " + v.playbackRate };
          case "next":
            if (yt && (click(".ytp-next-button") || click("ytmusic-player-bar .next-button"))) return { media: "next" };
            if (click("[aria-label*='Next' i], [data-testid='control-button-skip-forward']")) return { media: "next" };
            throw new Error("I couldn't find a next button on this page.");
          case "previous":
            if (yt && click(".ytp-prev-button")) return { media: "previous" };
            if (click("[aria-label*='Previous' i], [data-testid='control-button-skip-back']")) return { media: "previous" };
            history.back(); return { media: "went back" };
          case "fullscreen": {
            if (yt && click(".ytp-fullscreen-button")) return { media: "fullscreen toggled" };
            if (document.fullscreenElement) { document.exitFullscreen(); return { media: "exited fullscreen" }; }
            throw new Error("needs_keyboard");
          }
          default: throw new Error("Unsupported media action: " + op);
        }
      }

      case "scroll": {
        // Many sites (YouTube, Gmail, WhatsApp Web, docs...) scroll inside an inner container, so window.scrollBy
        // does nothing there. Find the real scrolling area, scroll it instantly, and report whether it MOVED.
        const d = cmd.direction || "down";
        const el = findScroller();
        const before = el.scrollTop;
        const amount = Math.round((el === document.scrollingElement ? window.innerHeight : el.clientHeight) * 0.8);
        if (d === "top") el.scrollTo({ top: 0, behavior: "auto" });
        else if (d === "bottom") el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
        else el.scrollBy({ top: d === "up" ? -amount : amount, behavior: "auto" });
        const moved = Math.abs(el.scrollTop - before) > 1;
        return { scrolled: d, moved };
      }

      default:
        throw new Error("Unsupported browser action: " + cmd.action);
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type !== "VAANI_ACTION") return false;
    try {
      sendResponse(perform(message.command || {}));
    } catch (error) {
      sendResponse({ error: String(error.message || error) });
    }
    return false;   // responded synchronously
  });
})();