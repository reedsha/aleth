// Frontend startup contract: proves a failing startup step cannot disable the UI.
//
// The app used to render normally but ignore every click when one startup step threw
// before the event wiring ran -- most plausibly canvas.getContext("2d") returning null
// on a GPU/WebView2 hiccup. The orb canvas is gone, but the shape of the failure it
// exposed is not: the step that renders the active plan can still throw on a plan the
// parser chokes on. That is not reproducible on demand either, so it is simulated here.
// Run from the repo root with no dependencies:
//
//   node tests/ui_startup_contract.js
//
// Exits non-zero if the wiring order or the visible error reporting regresses,
// or if ui/index.html drifts from its sources in ui/js/ and ui/css/.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const MODULES = [
  "state.js", "dom.js", "bootstrap.js", "plan-tree.js", "actions.js",
  "plan-modals.js", "agents.js", "workspace.js", "agent-events.js",
  "visuals.js", "dock.js", "workbench.js", "code-surface.js", "console.js",
  "preview.js", "sidebar.js", "env.js", "settings.js", "command-palette.js",
  "result-view.js", "wire.js",
];

// Mirrors tools/build_ui_bundle.py. A stylesheet that fails to load only costs
// decoration, but a stylesheet that goes missing through drift is still a silent
// regression, so the bundle is pinned here too.
const STYLESHEETS = [
  "base.css", "sidebar.css", "stage.css", "actions.css", "plan-tree.css",
  "modals.css", "tiered-prompt-editor.css", "ui-vision.css", "audit-modal.css",
  "rollback-modal.css", "dock.css", "workbench.css", "code-surface.css",
  "console.css", "preview.css", "sidebar-panels.css", "settings.css",
  "command-palette.css", "result-view.css",
];

function makeElement(id, ctxFactory) {
  let html = "";
  const el = {
    id,
    className: "",
    style: {},
    dataset: {},
    children: [],
    listeners: {},
    textContent: "",
    value: "",
    title: "",
    scrollHeight: 0,
    scrollTop: 0,
    clientHeight: 0,
    files: [],
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

  // `innerHTML = ""` really empties the element, the way a browser does, so a renderer that
  // re-renders by clearing its container does not accumulate stale children here.
  Object.defineProperty(el, "innerHTML", {
    get() { return html; },
    set(value) {
      html = String(value);
      if (html === "") el.children.length = 0;
    },
  });

  // A real class list, backed by `className` so the two never disagree. The tree's accordion
  // state and the sidebar's thinking dots are carried as classes, so a stub that silently
  // dropped them would let a broken renderer pass this contract.
  const classSet = () => new Set(String(el.className || "").split(/\s+/).filter(Boolean));
  const mutateClasses = (mutate) => {
    const set = classSet();
    mutate(set);
    el.className = [...set].join(" ");
  };
  el.classList = {
    add(...names) { mutateClasses((set) => names.forEach((n) => set.add(n))); },
    remove(...names) { mutateClasses((set) => names.forEach((n) => set.delete(n))); },
    contains(name) { return classSet().has(name); },
    toggle(name, force) {
      const on = force === undefined ? !classSet().has(name) : !!force;
      if (on) el.classList.add(name);
      else el.classList.remove(name);
      return on;
    },
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

// A stand-in for the sandbox's fake 2d context. No startup step draws to a canvas any
// more, so getContext is never reached in practice; the factory is kept so the
// harness's element scaffolding stays uniform.
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

// --- 0b. index.html must inline exactly the stylesheets in ui/css/. ----------
// Same class of failure as the modules: styles.css was split into ui/css/*.css
// and inlined so startup performs no stylesheet fetch either. Declaration order
// is the cascade, so the inline order must match the generator's exactly.
{
  const html = fs.readFileSync(path.join("ui", "index.html"), "utf8");
  const begin = html.indexOf("<!-- BEGIN STYLE BUNDLE");
  const end = html.indexOf("<!-- END STYLE BUNDLE -->");
  check("index.html contains the generated style bundle", begin !== -1 && end > begin,
    begin === -1 ? "missing BEGIN marker" : "");

  const region = begin === -1 ? "" : html.slice(begin, end);
  const inlined = [...region.matchAll(/  <style>\n([\s\S]*?)\n  <\/style>/g)].map((m) => m[1]);
  const onDisk = STYLESHEETS.map((name) =>
    fs.readFileSync(path.join("ui", "css", name), "utf8")
      .replace(/^\n+/, "")
      .replace(/\n+$/, ""));

  check("index.html inlines every stylesheet, in order", inlined.length === STYLESHEETS.length,
    `${inlined.length} inlined vs ${STYLESHEETS.length} on disk`);

  const stale = STYLESHEETS.filter((_name, i) => inlined[i] !== onDisk[i]);
  check("the inlined style bundle matches ui/css exactly", stale.length === 0,
    stale.length ? `stale: ${stale.join(", ")} (run: python tools/build_ui_bundle.py)` : "");

  const fetchesStyles = /<link[^>]+href="[^"]*\.css"/.test(html);
  check("index.html no longer fetches a local stylesheet", !fetchesStyles,
    fetchesStyles ? "found a <link ... .css> tag" : "");
}

// --- 0c. The element inventory dom.js caches must exist in the markup. -------
// Every element the frontend reaches for is captured by initDOMElements() from a
// fixed id, so the ids in ui/js/dom.js are the frontend's contract with the body
// markup. Relocating markup between panes (exactly what the UI redesign does) is
// the natural way to lose one by accident, and a lost id degrades to a silent
// no-op instead of an error. Pin the whole inventory here.
{
  const domCode = fs.readFileSync(path.join("ui", "js", "dom.js"), "utf8");
  const ids = [...new Set(
    [...domCode.matchAll(/getElementById\("([^"]+)"\)/g)].map((m) => m[1]))];

  check("dom.js caches a plausible element inventory", ids.length > 100,
    `${ids.length} ids`);

  // Only the hand-authored markup is searched: the generated JS bundle legitimately
  // builds some of these ids (e.g. #btnTreeExtract) as strings.
  const html = fs.readFileSync(path.join("ui", "index.html"), "utf8");
  const markup = html.slice(0, html.indexOf("<!-- BEGIN UI BUNDLE"));
  const countOf = (id) => (markup.match(new RegExp(`id="${id}"`, "g")) || []).length;

  const missing = ids.filter((id) => countOf(id) === 0);
  check("every id dom.js caches exists in the markup", missing.length === 0,
    missing.length ? `missing: ${missing.join(", ")}` : `${ids.length} ids present`);

  const duplicated = ids.filter((id) => countOf(id) > 1);
  check("no cached id is duplicated in the markup", duplicated.length === 0,
    duplicated.length ? `duplicated: ${duplicated.join(", ")}` : "");
}

// --- 0d. The class contracts that span modules are pinned by name. ----------
// These class names are the handshakes between the renderers, the delegated click
// handlers, and the styles: rename one in only half its sites and the control
// silently stops responding. Listing them here turns that rename into a failing
// test, so it has to be deliberate rather than an unnoticed casualty of a move.
{
  const html = fs.readFileSync(path.join("ui", "index.html"), "utf8");
  const markup = html.slice(0, html.indexOf("<!-- BEGIN UI BUNDLE"));
  const js = MODULES.map((m) => fs.readFileSync(path.join("ui", "js", m), "utf8")).join("\n");
  const css = STYLESHEETS.map((n) => fs.readFileSync(path.join("ui", "css", n), "utf8")).join("\n");
  const frontend = [markup, js, css].join("\n");

  const contractClasses = [
    "plan-tree-item", "plan-tree-section", "btn-inline-execute",
    "btn-inline-rollback", "btn-inline-edit", "btn-inline-fix", "agent-list-item",
    "active-agent",
    "sidebar-tab", "plan-chip", "log-line-tool", "log-line-decision", "preview-fail",
    "sidebar-tree", "tree-file-row", "tree-folder", "env-row", "env-reveal",
    "plan-summary-card", "sub-step-row",
    // The accordion tree's three tiers: a level-1 phase card, a level-2 step card and a
    // level-3 sub-deliverable row. Renaming one in only its renderer or only its stylesheet
    // would silently drop the tier's styling, so each is pinned by name.
    "plan-step-card", "plan-phase-header", "plan-substep-item",
  ];
  const absent = contractClasses.filter((name) => !frontend.includes(name));
  check("the cross-module class contracts are all present", absent.length === 0,
    absent.length ? `absent: ${absent.join(", ")}` : `${contractClasses.length} classes`);

  // The six intent buttons moved into the command palette, so the dock strip is gone and
  // the palette must expose the same six commands. Pin the new shape, not the old one.
  const dockButtons = (markup.match(/class="action-btn[ "]/g) || []).length;
  check("the dock button strip is gone", dockButtons === 0, `${dockButtons} found`);

  const palette = fs.readFileSync(path.join("ui", "js", "command-palette.js"), "utf8");
  const commands = (palette.match(/\{ id: "/g) || []).length;
  check("the command palette exposes six commands", commands === 6, `${commands} found`);
}

// --- 1. Healthy canvas: everything wires up. --------------------------------
{
  const { elements, fire } = load(workingContext);
  fire();
  const wired = countWiredListeners(elements);
  check("healthy 2d context wires the UI", wired > 0, `${wired} listeners`);
}

// --- 2. The plan workbench controls must be wired, even if one is missing. ----
// The workbench is the centre stage the redesign introduced: it renders the active
// plan and hosts the edit flow. A workbench that renders but ignores every click is
// the exact failure this contract exists to catch, and because its controls are wired
// from initEventListeners, one missing element must not cost the others.
{
  const wired = (run, id, type) =>
    ((run.elements.get(id) || { listeners: {} }).listeners[type] || []).length;

  const healthyRun = load(workingContext);
  healthyRun.fire();
  check("the plan workbench controls are wired",
    wired(healthyRun, "btnWorkbenchEdit", "click") === 1 &&
    wired(healthyRun, "btnWorkbenchSave", "click") === 1 &&
    wired(healthyRun, "btnWorkbenchDiscard", "click") === 1 &&
    wired(healthyRun, "planEditorInput", "input") === 1 &&
    wired(healthyRun, "planEditorInput", "scroll") === 1,
    `edit=${wired(healthyRun, "btnWorkbenchEdit", "click")}` +
    ` save=${wired(healthyRun, "btnWorkbenchSave", "click")}` +
    ` discard=${wired(healthyRun, "btnWorkbenchDiscard", "click")}` +
    ` input=${wired(healthyRun, "planEditorInput", "input")}` +
    ` scroll=${wired(healthyRun, "planEditorInput", "scroll")}`);

  const degradedRun = load(workingContext, new Set(["btnWorkbenchDiscard"]));
  degradedRun.fire();
  check("a missing workbench control does not cost the others",
    wired(degradedRun, "btnWorkbenchEdit", "click") === 1 &&
    wired(degradedRun, "planEditorInput", "input") === 1,
    `edit=${wired(degradedRun, "btnWorkbenchEdit", "click")}` +
    ` input=${wired(degradedRun, "planEditorInput", "input")}`);
}

// --- 3. A throwing startup step must not cost any wiring. -------------------
// initWorkbench renders the whole plan, so it is the step most likely to throw on a
// plan the parser chokes on. The workbench's own controls are wired by
// initEventListeners, which runs before it, so a throw here must cost nothing at all --
// that is what the ordering plus runStartupStep's isolation buys.
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
    sandbox.initWorkbench = () => { throw new Error("simulated startup failure"); };
    sandbox.initWorkbench();
    sandbox.initEventListeners();
    sandbox.initAutoScrollListeners();
    wired = countWiredListeners(elements);
  } catch (_err) {
    wired = countWiredListeners(elements);
  }
  check("old order: a throwing startup step left the UI completely dead", wired === 0, `listeners=${wired}`);
}

{
  // NEW order: the same throw is contained and reported, wiring is unaffected.
  const run = load(workingContext);
  run.sandbox.initWorkbench = () => { throw new Error("simulated startup failure"); };
  run.fire();

  const wired = countWiredListeners(run.elements);
  check("new order: a throwing startup step still wires everything", wired === healthyWiring,
    `healthy=${healthyWiring} wired=${wired}`);

  const banners = run.sandbox.document.body.children;
  const banner = banners[banners.length - 1];
  check("the failure is reported visibly in the window",
    banners.length > 0 && /Plan workbench failed/.test(banner.textContent || ""),
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
  run.sandbox.initWorkbench = () => { throw new Error("simulated startup failure"); };
  run.fire();

  check("startup failures are routed to the shared head reporter",
    reported.some((m) => /Plan workbench failed: simulated startup failure/.test(m)),
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
    broken.some((t) => /frontend modules did not load/.test(t) && /openActionDrawer/.test(t)),
    broken.join(" | ") || "no banner");
}

// --- 7. A missing bridge must not be disguised as a working app. ------------
// initFallbackMode used to seed a fake project whenever the bridge was absent, so a
// failed pywebview handshake produced a fully populated window showing a project the
// user does not have. Demo data is opt-in now; the default path must report instead.
{
  const run = load(workingContext);
  run.fire();
  const reported = [];
  run.sandbox.window.__deepAgentsReport = (m) => reported.push(String(m));
  run.sandbox.initFallbackMode();

  const ws = run.elements.get("txtWorkspacePath");
  check("a missing bridge is reported rather than disguised with demo data",
    reported.some((m) => /bridge unavailable/i.test(m)), JSON.stringify(reported));
  check("the fallback seeds no demo data without an explicit opt-in",
    !ws || !ws.textContent,
    ws ? `workspace label = ${JSON.stringify(ws.textContent)}` : "no element");
}

// --- 8. ?demo=1 still seeds the layout preview. ------------------------------
{
  const run = load(workingContext);
  run.fire();
  run.sandbox.window.location = { search: "?demo=1" };
  run.sandbox.initFallbackMode();

  const ws = run.elements.get("txtWorkspacePath");
  check("?demo=1 still seeds the preview data",
    !!ws && /my_project_workspace/.test(ws.textContent || ""),
    ws ? `workspace label = ${JSON.stringify(ws.textContent)}` : "no element");
}

// --- 9. The result view is a router on the action that ran. ------------------
// The centre stage used to render one summary shape for all six actions. A router that
// mounts nothing -- or the wrong renderer -- degrades into a blank overlay with no error
// at all, so the dispatch and the two payloads the terminal event does not carry (the
// architect summary and the pre-update roadmap) are pinned here. `state` and the module
// functions live in the sandbox's lexical scope, so a second script in the same context
// can drive them.
{
  const run = load(workingContext);
  run.fire();
  const body = run.elements.get("resultBody");
  const route = (script) => vm.runInContext(script, run.sandbox);

  route('state.selectedAction = "custom"; state.lastSummary = null; state.lastRunRefused = false; mountResultView();');
  check("a custom action still renders the plain stream", (body.innerHTML || "") === "",
    JSON.stringify(body.innerHTML));

  route('state.selectedAction = "recommend"; state.lastSummary = {status: "Advisory Formulated",' +
    ' proposals: ["Ship it", "Add a migration", "Write docs"], deliverables: ["Progress analyzed"]};' +
    ' mountResultView();');
  const cards = (body.innerHTML.match(/class="result-card"/g) || []).length;
  const addButtons = (body.innerHTML.match(/data-result-action="add-to-plan"/g) || []).length;
  check("a recommendation renders one card per proposal", cards === 3 && addButtons === 3,
    `cards=${cards} addButtons=${addButtons}`);

  route('state.selectedAction = "update_plan";' +
    ' state.planTreeBeforeUpdate = [{id:"t1",title:"Old",status:"pending"},{id:"t2",title:"Dropped",status:"pending"}];' +
    ' state.planTree = [{id:"t1",title:"Old",status:"completed"},{id:"t3",title:"Fresh",status:"pending"}];' +
    ' mountResultView();');
  const added = (body.innerHTML.match(/row-added/g) || []).length;
  const removed = (body.innerHTML.match(/row-removed/g) || []).length;
  const changed = (body.innerHTML.match(/row-changed/g) || []).length;
  check("a plan update renders the roadmap as a diff",
    added === 1 && removed === 1 && changed === 1,
    `added=${added} removed=${removed} changed=${changed}`);

  const revertButtons = (body.innerHTML.match(/data-result-action="revert-plan"/g) || []).length;
  check("a plan update offers a one-click revert of the saved revision", revertButtons === 1,
    `revert buttons=${revertButtons}`);

  const rendered = body.innerHTML;
  route('state.selectedAction = "recommend"; state.lastRunRefused = true; mountResultView();');
  check("a refusal is reported as a request for input, not as a result",
    body.innerHTML === rendered, "the refusal mounted a result view");

  // A verification gate that rejects the result is a finished run, not an approval: the
  // architect card must badge it as failed, and the transcript is pinned so the reason the
  // gate gave stays on screen after the run folds away.
  route('state.consolePinned = false;' +
    ' handleAgentEvent({type: "architect_summary", agent: "software-architect",' +
    ' summary: {title: "Task Approval", status: "Verification Failed", files: [],' +
    ' deliverables: ["Verification failed: tests did not pass (1 failed)"], proposals: []}});');
  const badge = run.elements.get("architectStatusBadge");
  const pinned = vm.runInContext("state.consolePinned", run.sandbox);
  check("a failed verification badges the architect card as failed, not verified",
    /status-badge failed/.test(badge.className || "") && (badge.textContent || "") === "Failed" &&
      pinned === true,
    `badge = ${JSON.stringify(badge.textContent)} / ${badge.className}`);

  // The runner reports a crash as workflow_complete(status="error") and a stray Stop as
  // workflow_stopped(status="stopped"). Neither may read as a clean finish (audit H3/H4/Go 15).
  route('finalizeWorkflow("error");');
  const errorLabel = run.elements.get("systemStatusLabel").textContent;
  check("an errored run is not reported as a clean finish",
    errorLabel === "Run Failed" &&
      /status-dot failed/.test(run.elements.get("systemStatusDot").className || ""),
    `label = ${JSON.stringify(errorLabel)}`);
  route('finalizeWorkflow("stopped");');
  const stoppedLabel = run.elements.get("systemStatusLabel").textContent;
  check("a halted run reports Halted, not Ready", stoppedLabel === "Halted",
    `label = ${JSON.stringify(stoppedLabel)}`);

  route('state.lastRunRefused = false; state.selectedAction = "analyze";' +
    ' state.lastSummary = {title: "Analysis", status: "Analysis Complete",' +
    ' files: ["a.py", "b.py"], deliverables: ["Evaluated workspace structure (2 files found)."]};' +
    ' handleAgentEvent({type: "workflow_complete", status: "finished"});');
  check("the terminal event mounts the action's own result view",
    /result-stat-value/.test(body.innerHTML) && /result-file-chip/.test(body.innerHTML),
    `mount kind = ${run.elements.get("resultKind").textContent}`);

  // The metrics are computed on the backend (tools/code_metrics.py); the dashboard has to
  // render them when they arrive, and say so when they do not, rather than implying a
  // clean bill of health either way.
  route('state.selectedAction = "analyze"; state.lastSummary = {title: "Analysis",' +
    ' status: "Analysis Complete", files: ["a.py"], deliverables: ["one finding"],' +
    ' metrics: {modules: 3, functions: 7, classes: 2, lines: 120, average_complexity: 2.4,' +
    ' max_complexity: 12, hotspot_threshold: 10, hotspot_count: 1,' +
    ' hotspots: [{file: "a.py", name: "big", line: 4, complexity: 12}],' +
    ' security_flag_count: 1, security_flags: [{file: "a.py", line: 9, kind: "eval"}],' +
    ' coverage: 1.0, unparsed: []}}; mountResultView();');
  check("the analyze dashboard renders the real source metrics",
    /Avg complexity/.test(body.innerHTML) && /Complexity hotspots/.test(body.innerHTML) &&
      /result-inline-code/.test(body.innerHTML) && /big\(\)/.test(body.innerHTML) &&
      /eval/.test(body.innerHTML),
    `stat tiles = ${(body.innerHTML.match(/result-stat-value/g) || []).length}`);

  route('state.selectedAction = "analyze"; state.lastSummary = {title: "Analysis",' +
    ' status: "Analysis Complete", files: ["a.py"], deliverables: ["one finding"]};' +
    ' mountResultView();');
  check("the analyze dashboard admits when it has no metrics",
    /No source metrics were computed/.test(body.innerHTML) &&
      !/Complexity hotspots/.test(body.innerHTML),
    `hotspot pane present = ${/Complexity hotspots/.test(body.innerHTML)}`);

  // The diff renderer is pure, so it is checked directly: the two file headers duplicate
  // the block header and are dropped, and the three line classes are what colour it.
  const diffHtml = vm.runInContext(
    'resultDiffLinesHtml(["--- a/x.py", "+++ b/x.py", "@@ -1,2 +1,2 @@", " keep", "-old", "+new"])',
    run.sandbox);
  check("the diff renderer drops the file headers and classes every line",
    !/--- a\//.test(diffHtml) && /diff-del">-old/.test(diffHtml) &&
      /diff-add">\+new/.test(diffHtml) && /diff-hunk/.test(diffHtml),
    diffHtml.slice(0, 160));

  // The pass/fail strip is driven by a real run now (tools/test_runner.py). Every branch
  // is checked directly, because the pill is pure: a pass is green, an assertion failure
  // is red, and anything that is not a clean run says so in amber rather than implying a
  // success that never happened.
  const pillFor = (payload, files) => vm.runInContext(
    `resultTestPill(${JSON.stringify(payload)}, ${JSON.stringify(files || [])})`, run.sandbox);
  const passPill = pillFor({ ran: true, found: true, verdict: "passed", totals: { passed: 2, failed: 0, errors: 0 } },
    [{ filename: "test_main.py" }]);
  check("a passing test run renders a green verdict",
    /result-pill-add/.test(passPill) && /2 passed/.test(passPill), passPill);

  const failPill = pillFor({ ran: true, found: true, verdict: "failed", totals: { passed: 1, failed: 2 } },
    [{ filename: "test_main.py" }]);
  check("a failing test run renders a red verdict",
    /result-pill-del/.test(failPill) && /1 passed, 2 failed/.test(failPill), failPill);

  const noRunnerPill = pillFor({ unavailable: true, ran: false, found: false }, [{ filename: "test_main.py" }]);
  check("an unreachable runner never renders as a pass",
    /result-pill-warn/.test(noRunnerPill) && /not run/.test(noRunnerPill) &&
      !/result-pill-add/.test(noRunnerPill), noRunnerPill);
}

// --- 10. The plan workbench: bento header + a three-tier accordion tree. ----
// The header became a fixed-height grid and the flat task list became a nested accordion,
// so the shape the renderer mounts is pinned here. The accordion's open state is the point
// of the check: renderPlanTree() rebuilds the tree wholesale on every plan load, save, sync
// and rollback, so a card that forgets its state the moment anything refreshes would look
// fine until the first background event and then silently fold itself.
{
  const run = load(workingContext);
  run.fire();
  const route = (script) => vm.runInContext(script, run.sandbox);

  const plan = {
    state_summary: { title: "Global State Summary", bullets: ["alpha context", "beta context"] },
    sections: [{
      id: "sec-1",
      title: "1. Setup",
      tasks: [{
        id: "task-1",
        title: "Scaffold the project",
        tag: "BE",
        status: "completed",
        details: ["create main.py"],
        sub_steps: [{ title: "init the repo", status: "completed" }],
      }],
    }],
  };
  route(`state.planJson = ${JSON.stringify(plan)};` +
    ' state.planTree = state.planJson.sections[0].tasks; renderPlanTree();');

  const tree = () => run.elements.get("planTreeContainer");
  const section = () => (tree().children || [])[0] || null;
  const header = () => (section() && section().children[0]) || null;
  const bodyEl = () => (section() && section().children[1]) || null;
  const stepCard = () => (bodyEl() && bodyEl().children[0]) || null;

  check("the tree nests a phase card, its body, and the step card inside it",
    !!section() && section().className === "plan-tree-section" &&
      !!header() && header().className === "plan-phase-header" &&
      !!bodyEl() && bodyEl().className === "plan-phase-body" &&
      !!stepCard() && stepCard().classList.contains("plan-tree-item") &&
      stepCard().classList.contains("plan-step-card"),
    `section=${section() && section().className}` +
    ` header=${header() && header().className}` +
    ` step=${stepCard() && stepCard().className}`);

  check("the phase header states its domain and its completion count",
    !!header() && /task-domain-pill/.test(header().innerHTML) &&
      /phase-count/.test(header().innerHTML) && /1\/1/.test(header().innerHTML),
    header() ? header().innerHTML.replace(/\s+/g, " ").slice(0, 130) : "no header");

  check("the step card keeps its action row and nests its level-3 rows",
    !!stepCard() && /plan-tree-item-row/.test(stepCard().innerHTML) &&
      /btn-inline-rollback/.test(stepCard().innerHTML) &&
      /plan-substep-item/.test(stepCard().innerHTML),
    stepCard() ? stepCard().innerHTML.replace(/\s+/g, " ").slice(0, 130) : "no card");

  const preview = run.elements.get("bentoSummaryPreview");
  check("the summary tile previews the plan's standing context",
    /alpha context/.test(preview.textContent) && /beta context/.test(preview.textContent),
    JSON.stringify(preview.textContent));

  const chips = run.elements.get("bentoStatsChips");
  check("the live tile reports the run state and the registry, inventing no metric",
    /Idle/.test(chips.innerHTML) && /0 architects/.test(chips.innerHTML) &&
      /1 task</.test(chips.innerHTML),
    chips.innerHTML);

  const progressChip = run.elements.get("bentoProgressChip");
  check("the progress tile badges a finished plan as complete",
    progressChip.textContent === "Complete" &&
      progressChip.classList.contains("state-complete") &&
      run.elements.get("planProgressBarFill").style.width === "100%",
    `${progressChip.className} / ${progressChip.textContent} / ` +
    `${run.elements.get("planProgressBarFill").style.width}`);

  route('state.expandedPlanTasks.add("task-1"); renderPlanTree();');
  check("an expanded step card survives a whole re-render",
    !!stepCard() && stepCard().classList.contains("expanded"),
    stepCard() ? stepCard().className : "no card");

  route('state.collapsedPlanPhases.add("sec-1"); renderPlanTree();');
  check("a collapsed phase survives a whole re-render",
    !!section() && section().classList.contains("collapsed"),
    section() ? section().className : "no section");
}

console.log(failures === 0 ? "\nALL CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
