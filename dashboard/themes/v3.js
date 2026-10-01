/* ==========================================================================
   v3 — "Bloom": motion and the top-bar furniture. Presentation only.

   Injected after dashboard.html's own script by `web.skinned` when
   DASHBOARD_THEME=v3. It never fetches, never changes state and never
   replaces a handler: the controls it moves into the top bar are the same
   nodes with the same listeners, and every animation is a Web Animation on
   transform / opacity / an SVG stroke, so nothing it does can block a click
   or shift layout.

   The rule that keeps it calm: a page *opening* is shown being drawn (cards
   rise in, lines stroke across, bars grow, donuts sweep, numbers count up).
   Anything re-rendered afterwards on that same visit -- a poll, a refresh
   after an action -- is not replayed; a number tweens from what it showed
   to what it shows now, and a chart simply swaps. Under
   prefers-reduced-motion everything is instant.
   ========================================================================== */
(() => {
  "use strict";
  const app = document.getElementById("app");
  if (!app || !document.documentElement.matches('[data-skin="v3"]')) return;

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const still = () => reduce.matches;
  const EASE = "cubic-bezier(.22, 1, .36, 1)";
  const rtl = () => document.documentElement.dir === "rtl";

  const play = (node, frames, opts) => {
    if (still() || !node.animate) return null;
    try { return node.animate(frames, { easing: EASE, fill: "backwards", ...opts }); } catch { return null; }
  };

  /* ---- top bar: search, language, theme, bell, profile --------------------- */

  function furnishTopbar() {
    const topbar = document.getElementById("topbar");
    if (!topbar || topbar.querySelector(".v3-tools")) return;
    const tools = document.createElement("div");
    tools.className = "v3-tools";

    const search = document.getElementById("paletteBtn");
    if (search) tools.append(search);
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.setAttribute("aria-hidden", "true");
    tools.append(sep);
    const lang = document.getElementById("langBtn");
    if (lang) { lang.classList.remove("sm", "ghost"); tools.append(lang); }
    const theme = document.getElementById("themeBtn");
    if (theme) { theme.classList.remove("sm", "ghost"); tools.append(theme); }

    // The bell is the review queue's own badge, shown where Mediline keeps
    // notifications. `setBadge` updates every [data-badge="queue"] on the
    // page, so it stays current with no code of its own.
    const bell = document.createElement("button");
    bell.type = "button";
    bell.className = "btn icon v3-bell";
    bell.hidden = true;
    const queueLabel = () => {
      const item = document.querySelector('.nav-item[data-route="queue"] .label');
      return item ? item.textContent : "";
    };
    bell.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg><span class="count" data-badge="queue" hidden></span>';
    bell.addEventListener("click", () => {
      const item = document.querySelector('.nav-item[data-route="queue"]');
      if (item) item.click();
    });
    tools.append(bell);

    const me = document.createElement("div");
    me.className = "v3-me";
    const avatar = document.getElementById("meAvatar");
    const who = document.querySelector(".rail-foot .who");
    if (avatar) me.append(avatar);
    if (who) me.append(who);
    tools.append(me);

    const actions = document.getElementById("pageActions");
    if (actions && actions.nextSibling) topbar.insertBefore(tools, actions.nextSibling);
    else topbar.append(tools);

    // A phone's top bar has room for the page's own actions, language, theme
    // and the bell -- not a search box and a profile chip as well. There the
    // search goes back above the menu and the profile back into the footer,
    // where the sidebar sheet shows them; the nodes move, nothing is copied.
    const rail = document.getElementById("rail");
    const nav = document.getElementById("nav");
    const foot = rail && rail.querySelector(".rail-foot");
    const phone = window.matchMedia("(max-width: 640px)");
    const place = () => {
      if (phone.matches) {
        if (search && nav) rail.insertBefore(search, nav);
        if (foot) { if (avatar) foot.prepend(avatar); if (who && avatar) avatar.after(who); }
        me.hidden = true;
      } else {
        if (search) tools.prepend(search);
        if (avatar) me.append(avatar);
        if (who) me.append(who);
        me.hidden = false;
      }
    };
    place();
    phone.addEventListener("change", place);

    // Shown only to an account that can open the queue; and the badge copied
    // from the sidebar's when the nav is (re)built, so a count already set
    // before this ran is not lost.
    const sync = () => {
      const source = document.querySelector('.nav-item [data-badge="queue"]');
      bell.hidden = !source;
      const label = queueLabel();
      if (label) { bell.title = label; bell.setAttribute("aria-label", label); }
      if (source) {
        const count = bell.querySelector(".count");
        count.hidden = source.hidden;
        count.textContent = source.textContent;
        count.dataset.tone = source.dataset.tone || "";
      }
    };
    sync();
    if (nav) new MutationObserver(sync).observe(nav, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"], characterData: true });
  }

  /* ---- the rail: an icon strip that opens on intent ----------------------------
     Shut by default on a desktop; hover (after a short intent delay) or
     keyboard focus opens it over the page, leaving closes it. The pin keeps
     it open and is remembered per signed-in user -- a display preference,
     the only thing this file stores. The shut geometry (.v3-shut) is applied
     only once the closing clip has finished, so nothing jumps while visible. */

  function railController() {
    const rail = document.getElementById("rail");
    const root = document.documentElement;
    if (!rail) return;
    const desktop = window.matchMedia("(min-width: 901px)");
    const userKey = () => {
      const name = document.getElementById("meName");
      return `rehla.v3.pin.${name ? name.textContent.trim() : ""}`;
    };
    const readPin = () => { try { return localStorage.getItem(userKey()) === "1"; } catch { return false; } };
    const writePin = (on) => { try { localStorage.setItem(userKey(), on ? "1" : "0"); } catch { /* private mode */ } };

    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = "v3-pin";
    pin.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.8V5h6v5.8l2.5 3.2H6.5Z"/><path d="M8 5h8"/></svg>';
    const pinLabel = () => (document.documentElement.lang === "en" ? "Keep the menu open" : "تثبيت القائمة مفتوحة");
    const brand = rail.querySelector(".brand");
    if (brand) brand.append(pin);

    let openTimer = 0;
    let closeTimer = 0;
    const pinned = () => root.classList.contains("v3-pinned");
    const open = () => {
      clearTimeout(closeTimer);
      if (!desktop.matches) return;
      rail.classList.remove("v3-shut");
      rail.classList.add("v3-open");
    };
    const shutNow = () => {
      if (!rail.classList.contains("v3-open") && !pinned()) rail.classList.add("v3-shut");
    };
    const close = () => {
      clearTimeout(openTimer);
      if (pinned() || !desktop.matches) return;
      rail.classList.remove("v3-open");
      // After the clip has closed (or at once, with no motion).
      clearTimeout(closeTimer);
      closeTimer = setTimeout(shutNow, still() ? 0 : 360);
    };
    const setPinned = (on, remember) => {
      root.classList.toggle("v3-pinned", on);
      pin.setAttribute("aria-pressed", String(on));
      pin.title = pinLabel();
      pin.setAttribute("aria-label", pinLabel());
      if (remember) writePin(on);
      if (on) { rail.classList.remove("v3-shut"); rail.classList.add("v3-open"); }
      else if (!rail.matches(":hover, :focus-within")) close();
    };

    rail.addEventListener("mouseenter", () => {
      clearTimeout(closeTimer);
      clearTimeout(openTimer);
      openTimer = setTimeout(open, still() ? 0 : 90);
    });
    rail.addEventListener("mouseleave", () => {
      clearTimeout(openTimer);
      closeTimer = setTimeout(close, 160);
    });
    rail.addEventListener("focusin", open);
    rail.addEventListener("focusout", (e) => { if (!rail.contains(e.relatedTarget)) close(); });
    // Choosing a section closes an unpinned overlay: the choice is made.
    rail.addEventListener("click", (e) => { if (e.target.closest(".nav-item") && !pinned()) { rail.classList.remove("v3-open"); closeTimer = setTimeout(shutNow, still() ? 0 : 360); } });
    pin.addEventListener("click", () => setPinned(!pinned(), true));

    // Names as tooltips, for the strip.
    const label = () => rail.querySelectorAll(".nav-item").forEach((item) => {
      const text = item.querySelector(".label");
      if (text && item.title !== text.textContent) item.title = text.textContent;
    });
    const nav = document.getElementById("nav");
    if (nav) new MutationObserver(label).observe(nav, { childList: true });
    label();

    const apply = () => {
      if (!desktop.matches) { rail.classList.remove("v3-open", "v3-shut"); root.classList.remove("v3-pinned"); return; }
      setPinned(readPin(), false);
      if (!pinned()) { rail.classList.remove("v3-open"); rail.classList.add("v3-shut"); }
    };
    apply();
    desktop.addEventListener("change", apply);
    // The signed-in name arrives after boot; the pin is theirs, so re-read it.
    const me = document.getElementById("meName");
    if (me) new MutationObserver(apply).observe(me, { childList: true, characterData: true, subtree: true });
  }

  /* ---- theme switch: a crossfade ---------------------------------------------- */

  let replaying = false;
  document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("#themeBtn, [data-theme-set]");
    if (!button || replaying || still() || !document.startViewTransition) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    const transition = document.startViewTransition(() => {
      replaying = true;
      try { button.click(); } finally { replaying = false; }
    });
    // Never let a stalled transition leave the page frozen on its snapshot.
    setTimeout(() => { try { transition.skipTransition(); } catch { /* done */ } }, 1200);
  }, true);

  /* ---- what counts as "opening" a page ------------------------------------------
     A visit starts when a view becomes active. For the next few seconds
     (long enough for its fetches to land) newly rendered content gets its
     entrance; after that, only things never seen on this visit do. */

  let visit = 0;
  let introUntil = 0;
  let seen = new Set();            // chart keys drawn on this visit
  const lastNumber = new Map();    // kpi key -> number last shown

  function startVisit() {
    visit += 1;
    introUntil = performance.now() + 2600;
    seen = new Set();
    const view = document.querySelector(".view.active");
    if (view) play(view, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }], { duration: 320 });
  }
  const inIntro = () => performance.now() < introUntil;

  const viewport = document.getElementById("viewport");
  if (viewport) {
    new MutationObserver((records) => {
      for (const r of records) {
        if (r.attributeName === "class" && r.target.classList.contains("view") &&
            r.target.classList.contains("active") && !(r.oldValue || "").includes("active")) {
          startVisit();
          return;
        }
      }
    }).observe(viewport, { attributes: true, attributeFilter: ["class"], attributeOldValue: true, subtree: true });
  }

  /* ---- charts ------------------------------------------------------------------ */

  function chartKey(svg) {
    const view = svg.closest(".view");
    const card = svg.closest(".card");
    const title = card && card.querySelector(".card-head h2");
    return `${view ? view.id : ""}|${title ? title.textContent : ""}|${svg.getAttribute("aria-label") || svg.className.baseVal}`;
  }

  /* A chart is drawn when it comes into view, not when it is rendered: one
     below the fold would otherwise finish before anybody scrolled to it.
     Until then it holds its first frame (set inline, so nothing flashes);
     the animation takes over from that frame and the inline style goes. */
  const hold = (node, prop, value) => { node.style[prop] = value; };
  const release = (node, prop, frames, opts) => {
    const anim = play(node, frames, opts);
    node.style[prop] = "";
    return anim;
  };
  const io = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      io.unobserve(e.target);
      const go = e.target.__v3go;
      delete e.target.__v3go;
      if (go) go();
    });
  }, { threshold: 0.2 }) : null;
  function whenVisible(svg, go) {
    if (!io) { go(); return; }
    svg.__v3go = go;
    io.observe(svg);
  }

  let clipSeq = 0;
  function drawLine(svg) {
    const lines = svg.querySelectorAll("path");
    if (!lines.length) return;
    const vb = svg.viewBox.baseVal;
    const NS = "http://www.w3.org/2000/svg";
    const id = `v3clip${(clipSeq += 1)}`;
    const clip = document.createElementNS(NS, "clipPath");
    clip.id = id;
    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", 0); rect.setAttribute("y", -10);
    rect.setAttribute("width", vb.width || 720); rect.setAttribute("height", (vb.height || 200) + 20);
    // The time axis runs left to right in both languages (the chart itself is
    // `direction: ltr`), so the line is drawn from its first day onward.
    rect.style.transformBox = "fill-box";
    rect.style.transformOrigin = "0 0";
    hold(rect, "transform", "scaleX(0)");
    clip.append(rect);
    let defs = svg.querySelector("defs");
    if (!defs) { defs = document.createElementNS(NS, "defs"); svg.prepend(defs); }
    defs.append(clip);
    lines.forEach((p) => p.setAttribute("clip-path", `url(#${id})`));
    const labels = holdAxis(svg);
    const done = () => { lines.forEach((p) => p.removeAttribute("clip-path")); clip.remove(); };
    whenVisible(svg, () => {
      const anim = release(rect, "transform", [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }],
        { duration: 900, delay: 80, easing: "cubic-bezier(.45, .05, .25, 1)" });
      if (anim) anim.finished.then(done, done); else done();
      showAxis(labels, 650);
    });
  }

  function growBars(svg) {
    const bars = Array.from(svg.querySelectorAll("rect.bar"));
    const step = Math.min(45, 520 / Math.max(bars.length, 1));
    bars.forEach((bar) => {
      bar.style.transformBox = "fill-box";
      bar.style.transformOrigin = "50% 100%";
      hold(bar, "transform", "scaleY(0)");
    });
    const labels = holdAxis(svg);
    whenVisible(svg, () => {
      bars.forEach((bar, i) => release(bar, "transform",
        [{ transform: "scaleY(0)" }, { transform: "scaleY(1)" }], { duration: 620, delay: 60 + i * step }));
      showAxis(labels, 250);
    });
  }

  function sweepDonut(svg) {
    const arcs = Array.from(svg.querySelectorAll("circle[stroke-dasharray]"));
    if (!arcs.length) return;
    const parts = arcs.map((a) => a.getAttribute("stroke-dasharray").split(/\s+/).map(Number));
    const total = parts.reduce((sum, [len]) => sum + len, 0) || 1;
    arcs.forEach((arc, i) => hold(arc, "strokeDasharray", `0 ${parts[i][0] + parts[i][1]}`));
    const label = svg.querySelector("text");
    if (label) hold(label, "opacity", "0");
    const wrap = svg.closest(".donutwrap");
    const legend = wrap ? Array.from(wrap.children).slice(1) : [];
    whenVisible(svg, () => {
      const SWEEP = 850;
      let at = 0;
      arcs.forEach((arc, i) => {
        const [len, gap] = parts[i];
        const share = (len / total) * SWEEP;
        release(arc, "strokeDasharray", [
          { strokeDasharray: `0 ${len + gap}` },
          { strokeDasharray: `${len} ${gap}` },
        ], { duration: Math.max(share, 90), delay: 80 + at, easing: i === arcs.length - 1 ? EASE : "linear" });
        at += share;
      });
      if (label) release(label, "opacity", [{ opacity: 0, transform: "scale(.85)" }, { opacity: 1, transform: "none" }], { duration: 400, delay: 450 });
      legend.forEach((node) => play(node, [{ opacity: 0 }, { opacity: 1 }], { duration: 400, delay: 600 }));
    });
  }

  function holdAxis(svg) {
    const labels = Array.from(svg.querySelectorAll(".axis text, .grid text"));
    labels.forEach((t) => hold(t, "opacity", "0"));
    return labels;
  }
  function showAxis(labels, delay) {
    labels.forEach((t) => release(t, "opacity", [{ opacity: 0 }, { opacity: 1 }], { duration: 350, delay }));
  }

  function animateChart(svg) {
    if (svg.dataset.v3) return;
    svg.dataset.v3 = "1";
    const key = chartKey(svg);
    const first = !seen.has(key);
    seen.add(key);
    if (!first) {
      // Data changed under an open page: a soft swap, nothing redrawn.
      play(svg, [{ opacity: .55 }, { opacity: 1 }], { duration: 260 });
      return;
    }
    if (still()) return;
    if (svg.classList.contains("donut")) sweepDonut(svg);
    else if (svg.querySelector("rect.bar")) growBars(svg);
    else drawLine(svg);
  }

  /* ---- numbers ------------------------------------------------------------------- */

  const NUMBER = /-?\d[\d,]*(?:\.\d+)?/;
  function animateNumber(node, delay) {
    if (node.dataset.v3) return;
    node.dataset.v3 = "1";
    const text = node.textContent;
    const match = text.match(NUMBER);
    const card = node.closest(".kpi, .card");
    const label = card && card.querySelector(".kpi-label");
    const view = node.closest(".view");
    const key = `${view ? view.id : ""}|${label ? label.textContent.trim() : ""}`;
    if (!match) { lastNumber.delete(key); return; }
    const target = Number(match[0].replace(/,/g, ""));
    if (!Number.isFinite(target)) return;
    const decimals = (match[0].split(".")[1] || "").length;
    const grouped = match[0].includes(",") || Math.abs(target) >= 1000;
    const before = text.slice(0, match.index);
    const after = text.slice(match.index + match[0].length);
    const fmt = new Intl.NumberFormat("en-US", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: grouped,
    });
    const from = lastNumber.has(key) ? lastNumber.get(key) : (inIntro() ? 0 : target);
    lastNumber.set(key, target);
    if (still() || from === target) return;
    const duration = lastNumber.size && from !== 0 ? 600 : 900;
    const start = performance.now() + delay;
    const tick = (now) => {
      if (!node.isConnected || node.textContent !== current) return; // re-rendered: theirs wins
      const t = Math.min(1, Math.max(0, (now - start) / duration));
      const eased = 1 - Math.pow(1 - t, 3);
      current = before + fmt.format(from + (target - from) * eased) + after;
      node.textContent = current;
      if (t < 1) requestAnimationFrame(tick);
    };
    // Reserve the final number's width before counting, so a value growing
    // from "0" never pushes into the icon or the label beside it.
    try {
      const range = document.createRange();
      range.selectNodeContents(node);
      const width = Math.ceil(range.getBoundingClientRect().width);
      if (width) node.style.minInlineSize = `${width}px`;
    } catch { /* measuring is a nicety */ }
    let current = before + fmt.format(from) + after;
    node.textContent = current;
    requestAnimationFrame(tick);
  }

  /* ---- entrances ----------------------------------------------------------------- */

  let staggerIndex = 0;
  let staggerReset = 0;
  function nextDelay() {
    const now = performance.now();
    if (now - staggerReset > 160) staggerIndex = 0;
    staggerReset = now;
    return Math.min(staggerIndex++ * 55, 440);
  }

  function enterCard(card) {
    if (card.dataset.v3in) return;
    card.dataset.v3in = "1";
    const delay = nextDelay();
    play(card, [{ opacity: 0, transform: "translateY(14px) scale(.985)" }, { opacity: 1, transform: "none" }], { duration: 520, delay });
    card.querySelectorAll(".chip").forEach((chip, i) =>
      play(chip, [{ opacity: 0, transform: "scale(.6)" }, { opacity: 1, transform: "none" }],
        { duration: 340, delay: delay + 220 + Math.min(i, 8) * 30, easing: "cubic-bezier(.34, 1.56, .64, 1)" }));
    return delay;
  }

  /* Conversations already listed on this visit; a new id slides in. */
  let knownConvs = null;
  function handleConvs(nodes) {
    const ids = nodes.map((n) => n.dataset.id);
    if (knownConvs === null || inIntro()) {
      if (knownConvs === null && inIntro()) nodes.slice(0, 14).forEach((n, i) =>
        play(n, [{ opacity: 0, transform: `translateX(${rtl() ? 12 : -12}px)` }, { opacity: 1, transform: "none" }], { duration: 380, delay: i * 30 }));
      knownConvs = new Set(ids);
      return;
    }
    nodes.forEach((n) => {
      if (!knownConvs.has(n.dataset.id)) {
        play(n, [{ opacity: 0, transform: "translateY(-10px)" }, { opacity: 1, transform: "none" }], { duration: 420 });
        n.querySelectorAll(".chip").forEach((chip) =>
          play(chip, [{ opacity: 0, transform: "scale(.6)" }, { opacity: 1, transform: "none" }], { duration: 320, delay: 160, easing: "cubic-bezier(.34, 1.56, .64, 1)" }));
      }
    });
    ids.forEach((id) => knownConvs.add(id));
  }

  /* Bubbles: the thread is re-rendered whole (one append per message), so
     "new" means "the same conversation, now longer by a few". Opening a
     thread, switching threads or a reload that changes nothing animates none. */
  let lastThread = null;
  let lastCount = 0;
  function handleLog(log) {
    const bubbles = Array.from(log.querySelectorAll(".bubble"));
    if (!bubbles.length) return; // a skeleton or an empty thread: keep the last count
    const selected = document.querySelector(".conv[aria-selected=\"true\"]");
    const thread = selected ? selected.dataset.id : null;
    const fresh = thread !== null && thread === lastThread ? bubbles.length - lastCount : 0;
    lastThread = thread;
    lastCount = bubbles.length;
    if (fresh < 1 || fresh > 4) return;
    bubbles.slice(-fresh).forEach((b, i) => {
      const own = b.classList.contains("user");
      const dx = (own ? -1 : 1) * (rtl() ? -1 : 1) * 16;
      play(b, [{ opacity: 0, transform: `translate(${dx}px, 10px) scale(.97)` }, { opacity: 1, transform: "none" }], { duration: 380, delay: i * 70 });
    });
  }

  /* ---- the observer that ties it together ---------------------------------------- */

  let pending = [];
  let scheduled = false;
  function flush() {
    scheduled = false;
    const batch = pending;
    pending = [];
    const convs = [];
    let log = null;
    for (const { node, parent } of batch) {
      if (!node.isConnected || node.nodeType !== 1) continue;
      if (node.closest(".skel") || node.classList.contains("skel")) continue;

      if (parent && parent.id === "log") { log = parent; continue; }
      if (node.classList.contains("conv")) { convs.push(node); continue; }
      node.querySelectorAll?.(".conv").forEach((c) => convs.push(c));

      const cards = node.matches(".card") ? [node] : Array.from(node.querySelectorAll(".card"));
      if (inIntro()) cards.forEach((card) => { if (!card.closest(".drawer, .modal")) enterCard(card); });

      const values = node.matches(".kpi-value") ? [node] : Array.from(node.querySelectorAll(".kpi-value"));
      values.forEach((v) => {
        const card = v.closest(".card");
        animateNumber(v, card && card.dataset.v3in ? 150 : 0);
      });

      const charts = node.matches("svg.chart:not(.spark), svg.donut") ? [node]
        : Array.from(node.querySelectorAll("svg.chart:not(.spark), svg.donut"));
      charts.forEach(animateChart);
    }
    if (convs.length) handleConvs(convs);
    if (log) handleLog(log);
  }

  function observeContent(root) {
    new MutationObserver((records) => {
      for (const r of records) {
        r.addedNodes.forEach((node) => pending.push({ node, parent: r.target }));
      }
      // A microtask, not a frame: the first frame is held before the browser
      // paints, so nothing is ever seen drawn and then undrawn.
      if (!scheduled && pending.length) { scheduled = true; queueMicrotask(flush); }
    }).observe(root, { childList: true, subtree: true });
  }

  /* Thread changes reset what "new" means for bubbles: an entire thread
     loading is one big batch and is left alone by handleBubbles anyway. */

  function boot() {
    furnishTopbar();
    railController();
    if (viewport) observeContent(viewport);
    if (!app.hidden) startVisit();
    else {
      new MutationObserver((_, obs) => {
        if (!app.hidden) { obs.disconnect(); startVisit(); }
      }).observe(app, { attributes: true, attributeFilter: ["hidden"] });
    }
  }
  boot();
})();
