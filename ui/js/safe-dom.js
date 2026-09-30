// ui/js/safe-dom.js — the single choke point for HTML insertion.
//
// Why this module exists
// ----------------------
// The UI builds markup with template strings and, before this module, assigned it with
// `innerHTML` in ~60 places across ~14 modules. Each of those was a place where a missed
// `escapeHtml()` -- or a field that never had one at all, like `agent.display_name` -- became
// script execution in the app's own origin: `innerHTML` parses its argument as HTML, so a
// string that reaches it with a `<script>` or an `on*` handler is live markup, not text.
//
// Routing every insertion through one allowlist-based sanitiser means a missed escape can no
// longer execute: DOMPurify drops anything the app's own templates do not generate, and this
// is the only module in `ui/js` that touches `innerHTML`. It is a *leaf* in the module graph
// (it imports only DOMPurify), so nothing here can introduce a cycle.
//
// The allowlists are derived from the app's own templates -- `grep -rhoE "<[a-zA-Z][a-zA-Z0-9]*" ui/js`
// and the attribute names in those templates -- rather than inherited from DOMPurify's
// defaults, so the permitted set is stated rather than implied. Everything script-capable is
// named explicitly in FORBID_TAGS/FORBID_ATTR as well, even where DOMPurify already refuses
// it, so a future config change cannot quietly re-admit it:
//
//   * `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, `<link>`, `<meta>`
//     and `<base>` are forbidden tags;
//   * event-handler attributes (`on*`) and `srcdoc` are forbidden attributes. `on*` is
//     refused because it is simply absent from the allowlist, which is the same mechanism
//     that refuses any attribute the app does not generate.

import DOMPurify from "dompurify";

// ---------------------------------------------------------------------------
// The strict allowlist: exactly the markup ui/js generates
// ---------------------------------------------------------------------------

const APP_TAGS = [
  // Structure and text
  "div", "span", "p", "br", "hr", "strong", "em", "b", "i",
  "code", "pre", "h1", "h2", "h3", "h4",
  // Lists
  "ul", "ol", "li",
  // Interactive controls (the settings form and the plan cards)
  "button", "input", "select", "option", "label",
  // Tables (named for the markup a future renderer is expected to add)
  "table", "thead", "tbody", "tr", "td", "th",
  // Containers
  "section", "article", "details", "summary",
  // SVG icons: the app's own inline icon set
  "svg", "g", "defs", "use",
  "path", "polygon", "polyline", "line", "circle", "ellipse", "rect"
];

const APP_ATTRS = [
  "class", "id", "title", "style", "role",
  "type", "value", "placeholder", "disabled", "checked", "selected",
  "for", "name", "spellcheck", "autocomplete", "hidden", "open",
  "colspan", "rowspan", "scope",
  // SVG geometry and presentation. `viewBox` is matched case-insensitively by DOMPurify and
  // kept verbatim in the output, so the icon set still scales.
  "width", "height", "viewBox",
  "d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
  "points", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry"
];

// `data-*` and `aria-*` are permitted by DOMPurify's own switches rather than by listing
// every name; the app uses both heavily (data-task-id, aria-expanded, ...).
const APP_FORBIDDEN_TAGS = [
  "script", "style", "iframe", "object", "embed", "form", "link", "meta", "base",
  "noscript", "template", "frame", "frameset", "applet", "math", "foreignobject"
];

const APP_FORBIDDEN_ATTRS = ["srcdoc", "formaction", "xlink:href", "srcset"];

export const STRICT_CONFIG = {
  ALLOWED_TAGS: APP_TAGS,
  ALLOWED_ATTR: APP_ATTRS,
  FORBID_TAGS: APP_FORBIDDEN_TAGS,
  FORBID_ATTR: APP_FORBIDDEN_ATTRS,
  ALLOW_DATA_ATTR: true,
  ALLOW_ARIA_ATTR: true,
  KEEP_CONTENT: true
};

// ---------------------------------------------------------------------------
// The preview allowlist: a whole document the workspace generated
// ---------------------------------------------------------------------------
//
// The live preview renders `ui_view.html` -- a complete HTML document produced in the
// workspace, not app markup -- so it needs the document and presentation tags the strict
// list deliberately omits: `<html>/<head>/<body>/<title>` for the document, and `<style>`
// so the generated interface still looks like itself. Everything script-capable or
// navigational stays forbidden, and the iframe that receives this document is sandboxed
// without `allow-scripts` and without `allow-same-origin` (see ui/js/preview.js), which is
// the primary containment; this sanitiser is the second layer. `<style>` cannot execute
// script, and the frame it applies inside is an opaque origin with no access to the app.

const PREVIEW_TAGS = [
  "html", "head", "body", "title", "style",
  "div", "span", "section", "header", "footer", "nav", "main", "aside", "article",
  "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "blockquote", "pre", "code",
  "ul", "ol", "li", "dl", "dt", "dd",
  "strong", "em", "b", "i", "u", "s", "small", "mark", "sub", "sup", "abbr", "cite", "q",
  "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
  "figure", "figcaption", "img",
  "label", "button", "input", "select", "option", "optgroup", "textarea",
  "details", "summary", "progress", "meter", "time", "address",
  "svg", "g", "defs", "symbol", "use", "path", "polygon", "polyline", "line",
  "circle", "ellipse", "rect", "text", "tspan", "marker"
];

const PREVIEW_ATTRS = [
  "class", "id", "title", "style", "role", "lang", "dir",
  "href", "src", "alt", "target", "rel", "width", "height",
  "type", "value", "placeholder", "disabled", "checked", "selected",
  "for", "name", "spellcheck", "autocomplete", "hidden", "open",
  "cols", "rows", "min", "max", "step",
  "colspan", "rowspan", "scope", "headers",
  "viewBox", "preserveAspectRatio", "d", "fill", "stroke",
  "stroke-width", "stroke-linecap", "stroke-linejoin", "points",
  "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
  "transform", "opacity", "fill-rule", "clip-rule"
];

const PREVIEW_CONFIG = {
  WHOLE_DOCUMENT: true,
  ALLOWED_TAGS: PREVIEW_TAGS,
  ALLOWED_ATTR: PREVIEW_ATTRS,
  FORBID_TAGS: [
    "script", "iframe", "object", "embed", "form", "link", "meta", "base",
    "noscript", "template", "frame", "frameset", "applet", "math", "foreignobject",
    "audio", "video", "source", "track", "canvas"
  ],
  FORBID_ATTR: ["srcdoc", "formaction", "xlink:href"],
  ALLOW_DATA_ATTR: true,
  ALLOW_ARIA_ATTR: true,
  KEEP_CONTENT: true
};

function sanitize(html, config) {
  const source = html === null || html === undefined ? "" : String(html);
  return DOMPurify.sanitize(source, config);
}

// ---------------------------------------------------------------------------
// The insertion API
// ---------------------------------------------------------------------------

/**
 * Sanitises `html` against the strict allowlist and assigns it to `el`.
 *
 * This is the only place in `ui/js` that writes `innerHTML`. Callers keep building markup
 * with template strings exactly as before; they just hand it here instead of to the DOM.
 */
export function setHtml(el, html) {
  if (!el) return;
  el.innerHTML = sanitize(html, STRICT_CONFIG);
}

/** Assigns `text` as a text node, so it can never be parsed as markup. */
export function setText(el, text) {
  if (!el) return;
  el.textContent = text === null || text === undefined ? "" : String(text);
}

/**
 * Sanitises a whole workspace-generated document for the live preview's iframe.
 *
 * Returns a complete document (a DOCTYPE is re-attached if the sanitiser dropped it) so the
 * frame parses it in standards mode rather than quirks mode.
 */
export function sanitizePreviewHtml(html) {
  const clean = sanitize(html, PREVIEW_CONFIG);
  return /^\s*<!doctype/i.test(clean) ? clean : `<!DOCTYPE html>\n${clean}`;
}
