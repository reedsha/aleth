// Frontend startup contract: proves a decorative failure cannot disable the UI.
//
// The app used to render normally but ignore every click when one startup step threw
// before the event wiring ran -- most plausibly canvas.getContext("2d") returning null
// on a GPU/WebView2 hiccup. That failure is not reproducible on demand, so it is
// simulated here. Run from the repo root with no dependencies:
//
//   node tests/ui_startup_contract.js
//
// Exits non-zero if the wiring order or the visible error reporting regresses.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const MODULES = [
  "state.js", "dom.js", "bootstrap.js", "plan-tree.js", "actions.js",
  "plan-modals.js", "agents.js", "workspace.js", "agent-events.js",
  "visuals.js", "wire.js",
];

function makeElement(id, ctxFactory) {
  const el = {
    id,
    style: {},
    dataset: {},
    children: [],
    listeners: {},
    textContent: "",
    innerHTML: "",
    value: "",
    title: "",
    scrollHeight: 0,
    scrollTop: 0,
    clientHeight: 0,
    files: [],
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.push(child); return child; },
    remove() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getContext() { return ctxFactory(); },
    getBoundingClientRect() { return { width: 300, height: 300, top: 0, left: 0 }; },
    focus() {},
    blur() {},
    click() {},
    closest() { return null; },
    contains() { return false; },
  };
  return el;
}

function makeSandbox(ctxFactory, missingIds) {
  const elements = new Map();
  const document = {
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    getElementById(id) {
      // Simulates an element absent from the document, which is what used to abort
      // the remainder of initEventListeners.
      if (missingIds && missingIds.has(id)) return null;
      if (!elements.has(id)) elements.set(id, makeElement(id, ctxFactory));
      return elements.get(id);
    },
    createElement(tag) { return makeElement(tag, ctxFactory); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    body: makeElement("body", ctxFactory),
    documentElement: makeElement("html", ctxFactory),
  };

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    setTimeout: () => 0,
    clearTimeout: () => {},
    requestAnimationFrame: () => 0,
    addEventListener() {},
    document,
    pywebview: undefined,
  };
  // In a browser `window` IS the global object, and the modules declare their entry
  // points on it as top-level function declarations. Modelling `window` as a separate
  // object would make a `typeof window[name]` guard look broken when it is not.
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return { sandbox, document, elements };
}

// A stand-in for window.pywebview.api, unused here but keeps the bridge branch quiet.
function load(ctxFactory, missingIds) {
  const { sandbox, document, elements } = makeSandbox(ctxFactory, missingIds);
  const code = MODULES
    .map((m) => fs.readFileSync(path.join("ui", "js", m), "utf8"))
    .join("\n;\n");
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: "bundle.js" });

  const onReady = document.listeners.DOMContentLoaded || [];
  if (onReady.length !== 1) {
    throw new Error(`expected exactly 1 DOMContentLoaded listener, got ${onReady.length}`);
  }
  return { sandbox, elements, fire: () => onReady[0]() };
}

// Count listeners attached to the cached DOM elements (the clickable controls).
// `const DOM` lives in the bundle's lexical scope, so wiring is observed through
// the elements the modules looked up instead.
function countWiredListeners(elements) {
  let total = 0;
  for (const el of elements.values()) {
    for (const type of Object.keys(el.listeners)) total += el.listeners[type].length;
  }
  return total;
}

function workingContext() {
  const gradient = { addColorStop() {} };
  return new Proxy({}, {
    get(_t, prop) {
      if (prop === "createLinearGradient" || prop === "createRadialGradient") return () => gradient;
      return () => {};
    },
    set() { return true; },
  });
}

let failures = 0;
function check(label, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? " -- " + detail : ""}`);
  if (!ok) failures++;
}

// --- 0. index.html must inline exactly the modules in ui/js/. ----------------
// Step 3 replaced one <script src="app.js"> with eleven module requests, and
// WebView2 intermittently failed individual ones: a lost module left the window
// rendered but inert. The modules are inlined now (see tools/build_ui_bundle.py),
// so index.html must never drift from ui/js, or the app would silently run stale
// or missing code -- the same class of failure in a new disguise.
{
  const html = fs.readFileSync(path.join("ui", "index.html"), "utf8");
  const begin = html.indexOf("<!-- BEGIN UI BUNDLE");
  const end = html.indexOf("<!-- END UI BUNDLE -->");
  check("index.html contains the generated UI bundle", begin !== -1 && end > begin,
    begin === -1 ? "missing BEGIN marker" : "");

  const region = begin === -1 ? "" : html.slice(begin, end);
  const inlined = [...region.matchAll(/  <script>\n([\s\S]*?)\n  <\/script>/g)].map((m) => m[1]);
  const onDisk = MODULES.map((m) =>
    fs.readFileSync(path.join("ui", "js", m), "utf8")
      .replace(/^\n+/, "")
      .replace(/\n+$/, ""));

  check("index.html inlines every module, in order", inlined.length === MODULES.length,
    `${inlined.length} inlined vs ${MODULES.length} on disk`);

  const stale = MODULES.filter((_name, i) => inlined[i] !== onDisk[i]);
  check("the inlined bundle matches ui/js exactly", stale.length === 0,
    stale.length ? `stale: ${stale.join(", ")} (run: python tools/build_ui_bundle.py)` : "");

  const fetchesModules = /<script src="js\//.test(html);
  check("index.html no longer fetches the modules", !fetchesModules,
    fetchesModules ? "found a <script src=\"js/...\"> tag" : "");
}

// --- 1. Healthy canvas: everything wires up. --------------------------------
{
  const { elements, fire } = load(workingContext);
  fire();
  const wired = countWiredListeners(elements);
  check("healthy 2d context wires the UI", wired > 0, `${wired} listeners`);
}

// --- 2. Broken canvas: the orb must not take the UI down with it. -----------
{
  const healthyRun = load(workingContext);
  healthyRun.fire();
  const healthy = countWiredListeners(healthyRun.elements);

  const brokenRun = load(() => null);
  brokenRun.fire();
  const broken = countWiredListeners(brokenRun.elements);

  check("null 2d context still wires the UI", broken > 0, `${broken} listeners`);
  check("degraded canvas wires exactly as many listeners as a healthy one", broken === healthy,
    `healthy=${healthy} broken=${broken}`);
}

// --- 3. A throwing decorative step must not cost any wiring. ---------------
// The orb guard handles a null 2d context, but decoration must be harmless even
// when it fails for a reason nobody anticipated (e.g. visuals.js never loaded, so
// initOrbAnimation is undefined). That is what the ordering + isolation buys.
const healthyWiring = (() => {
  const run = load(workingContext);
  run.fire();
  return countWiredListeners(run.elements);
})();

{
  // OLD order, no isolation: the throw escaped and skipped every step below it.
  const { sandbox, elements } = load(workingContext);
  let wired;
  try {
    sandbox.initDOMElements();
    sandbox.initOrbAnimation = () => { throw new Error("simulated decorative failure"); };
    sandbox.initOrbAnimation();
    sandbox.initEventListeners();
    sandbox.initAutoScrollListeners();
    wired = countWiredListeners(elements);
  } catch (_err) {
    wired = countWiredListeners(elements);
  }
  check("old order: a throwing orb left the UI completely dead", wired === 0, `listeners=${wired}`);
}

{
  // NEW order: the same throw is contained and reported, wiring is unaffected.
  const run = load(workingContext);
  run.sandbox.initOrbAnimation = () => { throw new Error("simulated decorative failure"); };
  run.fire();

  const wired = countWiredListeners(run.elements);
  check("new order: a throwing orb still wires everything", wired === healthyWiring,
    `healthy=${healthyWiring} wired=${wired}`);

  const banners = run.sandbox.document.body.children;
  const banner = banners[banners.length - 1];
  check("the failure is reported visibly in the window",
    banners.length > 0 && /Orb animation failed/.test(banner.textContent || ""),
    banner ? JSON.stringify(banner.textContent) : "no banner");
}

// --- 4. A missing element must not cost any other wiring. -------------------
// These listeners all live in one function, so a bare `DOM.x.addEventListener` on a
// missing element aborted the rest of it. That is exactly how a broken rollback modal
// presented: openRollbackModal (wired by the plan tree) still opened the overlay,
// while its close and confirm buttons were never wired -- leaving the window covered
// by an overlay that ignored every click.
{
  const run = load(workingContext, new Set(["btnSelectWorkspace"]));
  run.fire();

  const listenersOn = (id, type) =>
    ((run.elements.get(id) || { listeners: {} }).listeners[type] || []).length;
  const close = listenersOn("btnCloseRollbackModal", "click");
  const confirm = listenersOn("btnConfirmRollbackAction", "click");
  const checkbox = listenersOn("chkRollbackConfirm", "change");
  check("a missing element leaves the rollback modal fully wired",
    close === 1 && confirm === 1 && checkbox === 1,
    `close=${close} confirm=${confirm} checkbox=${checkbox}`);

  const banners = run.sandbox.document.body.children;
  check("the missing element is reported rather than swallowed",
    banners.some((b) => /element is missing/.test(b.textContent || "")),
    banners.map((b) => b.textContent).join(" | ") || "no banner");
}

// --- 5. Startup failures use the head reporter when it is installed. --------
// The document head installs a reporter before any module runs so that every failure
// lands in one stack of banners. wire.js must hand its own failures over to it.
{
  const run = load(workingContext);
  const reported = [];
  run.sandbox.window.__deepAgentsReport = (message) => reported.push(String(message));
  run.sandbox.initOrbAnimation = () => { throw new Error("simulated decorative failure"); };
  run.fire();

  check("startup failures are routed to the shared head reporter",
    reported.some((m) => /Orb animation failed: simulated decorative failure/.test(m)),
    JSON.stringify(reported));
  check("a routed failure does not also raise a local banner",
    run.sandbox.document.body.children.length === 0,
    `${run.sandbox.document.body.children.length} banner(s)`);
}

// --- 6. The head watchdog must be silent when healthy and name what is missing. ---
// The watchdog is the only code guaranteed to run, so it is the safety net when a
// module does not execute. It must not cry wolf on every launch, and when it does
// fire it has to name the missing entry points: "nothing works" is not actionable.
{
  const html = fs.readFileSync(path.join("ui", "index.html"), "utf8");
  const headCode = html.slice(0, html.indexOf("</head>"))
    .match(/<script>\n([\s\S]*?)\n  <\/script>/)[1];

  function runWithWatchdog(moduleFiles) {
    const { sandbox, document } = makeSandbox(workingContext);
    // Only zero-delay timers run: the watchdog uses one, whereas the 400ms offline
    // fallback would otherwise seed demo data part-way through the test.
    sandbox.setTimeout = (fn, delay) => { if (!delay) fn(); return 0; };
    vm.createContext(sandbox);
    vm.runInContext(headCode, sandbox, { filename: "head.js" });
    vm.runInContext(
      moduleFiles.map((m) => fs.readFileSync(path.join("ui", "js", m), "utf8")).join("\n;\n"),
      sandbox,
      { filename: "bundle.js" });
    for (const fn of document.listeners.DOMContentLoaded || []) fn();
    return document.body.children.map((b) => b.textContent || "");
  }

  const healthy = runWithWatchdog(MODULES);
  check("the head watchdog stays quiet when every module loads", healthy.length === 0,
    healthy.join(" | ") || "no banner");

  const broken = runWithWatchdog(MODULES.filter((name) => name !== "actions.js"));
  check("the head watchdog names a module that did not load",
    broken.some((t) => /frontend modules did not load/.test(t) && /openActionParamModal/.test(t)),
    broken.join(" | ") || "no banner");
}

console.log(failures === 0 ? "\nALL CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
