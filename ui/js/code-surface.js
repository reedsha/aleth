// ui/js/code-surface.js — Syntax highlighting for the parameter textareas.
//
// The action-parameter fields are where a directive is written, so they get the same
// treatment as the plan workbench: the field is underlined by a highlighted copy of its
// own text. The textarea keeps the caret, the selection and the keyboard, and its glyphs
// are made transparent so the copy underneath shows through. That only holds while the
// two layers break every line at the same character, so this module never inserts,
// removes or substitutes a character -- a token keeps its exact text and only gains a
// span. Wrapping is left to the browser; ui/css/code-surface.css pins every metric the
// two layers share, and the one it cannot pin -- the width the textarea's own scrollbar
// takes from its text -- is measured here and mirrored as padding.
//
// The wrapper is built here rather than in the markup, so no id or class in
// ui/index.html changes and a browser that never runs this keeps the plain textarea it
// has today.

// One pass, so a token this scan wraps can never be rescanned by a later alternative.
// Group order is the precedence: a line's own structure marker, then inline code, then a
// quoted literal, then a path or URL. The marker alternative is anchored with ^ and the
// pattern carries no m flag, so it only matches at the head of a line -- and because
// exec() advances lastIndex past index 0, it fires at most once per line.
const CODE_SURFACE_RE = new RegExp(
  [
    "(^\\s*(?:#{1,6}|[-*+]|\\d+[.)]|>)(?=\\s))",
    "(`[^`]*`)",
    "(\"[^\"\\n]*\"|'[^'\\n]*')",
    "(https?://\\S+|\\.{1,2}/[\\w.@/-]*|~/[\\w.@/-]*" +
      "|\\b[\\w.-]+\\.(?:py|js|mjs|cjs|ts|tsx|jsx|json|md|css|html|htm|rs|go|java|rb|php" +
      "|c|h|cpp|hpp|yaml|yml|toml|ini|cfg|conf|env|sh|bat|ps1|sql|txt|log|lock|csv|xml|svg)\\b)",
  ].join("|"),
  "g"
);

// Every branch is escaped as it is emitted, so the output is HTML-safe by construction
// and the only angle brackets in it are the spans built here.
function codeSurfaceLine(line) {
  const parts = [];
  let last = 0;
  let match;
  CODE_SURFACE_RE.lastIndex = 0;
  while ((match = CODE_SURFACE_RE.exec(line)) !== null) {
    if (match[0].length === 0) { // cannot happen with the pattern above; never spin if it does
      CODE_SURFACE_RE.lastIndex++;
      continue;
    }
    if (match.index > last) parts.push(escapeHtml(line.slice(last, match.index)));
    if (match[1] !== undefined) {
      parts.push(`<span class="cs-mark">${escapeHtml(match[1])}</span>`);
    } else if (match[2] !== undefined) {
      parts.push(`<span class="cs-code">${escapeHtml(match[2])}</span>`);
    } else if (match[3] !== undefined) {
      parts.push(`<span class="cs-string">${escapeHtml(match[3])}</span>`);
    } else {
      parts.push(`<span class="cs-path">${escapeHtml(match[4])}</span>`);
    }
    last = match.index + match[0].length;
  }
  parts.push(escapeHtml(line.slice(last)));
  return parts.join("");
}

function codeSurfaceHighlight(text) {
  return String(text === undefined || text === null ? "" : text)
    .split("\n")
    .map(codeSurfaceLine)
    .join("\n");
}

// A line of the copy is laid out in fragments -- one per token span -- and Chromium snaps
// each fragment's width to its layout grid, so a line whose glyphs exactly fill the field
// can measure a few hundredths of a pixel wider than the textarea's single fragment of the
// same glyphs, and wrap one character early. Half a pixel absorbs that rounding; it is far
// too little to fit another glyph, which is 7.2px wide at this size.
const CODE_SURFACE_FRAGMENT_SLACK = 0.5;

// Wraps one textarea in its highlight layer and returns the painters that keep the two
// in step. Returns null on anything unexpected: the field must stay fully usable as a
// plain textarea rather than half-enhanced.
function buildCodeSurface(textarea) {
  if (!textarea || textarea.dataset.codeSurface === "done") return null;
  const parent = textarea.parentNode;
  if (!parent || typeof parent.insertBefore !== "function" ||
      typeof document.createElement !== "function") {
    return null;
  }
  textarea.dataset.codeSurface = "done";

  const surface = document.createElement("div");
  surface.className = "cs-surface";
  const highlight = document.createElement("pre");
  highlight.className = "cs-highlight";
  highlight.setAttribute("aria-hidden", "true");

  // The textarea carries the cs-input half of the shared metrics, plus the transparency
  // that lets the copy underneath show through.
  textarea.classList.add("cs-input");
  parent.insertBefore(surface, textarea);
  surface.appendChild(highlight);
  surface.appendChild(textarea);

  // Chromium wraps a textarea's text inside its content box minus whatever its own
  // scrollbar takes, and this overlay has no scrollbar of its own: the gutter it could
  // reserve instead is a fraction of a pixel too narrow, and a fraction is enough to
  // rewrap a line one character early and drag every glyph after it out of place. So the
  // width the textarea actually lost is measured once, and mirrored as padding here. The
  // measurement needs a layout, so a dialog that has never been shown is skipped and the
  // next paint retries; the textarea's gutter keeps the width constant whether or not its
  // scrollbar is currently drawn, so one measurement holds for the life of the field.
  let reserved = "";
  const mirrorScrollbarWidth = () => {
    if (!reserved) {
      if (!textarea.clientWidth) return;
      const css = getComputedStyle(textarea);
      const lost = textarea.offsetWidth - textarea.clientWidth -
        parseFloat(css.borderLeftWidth) - parseFloat(css.borderRightWidth);
      if (!(lost > 0)) return;
      reserved = (parseFloat(css.paddingRight) + lost - CODE_SURFACE_FRAGMENT_SLACK) + "px";
    }
    if (highlight.style.paddingRight !== reserved) highlight.style.paddingRight = reserved;
  };

  const paint = () => {
    mirrorScrollbarWidth();
    highlight.innerHTML = codeSurfaceHighlight(textarea.value);
    highlight.scrollTop = textarea.scrollTop;
    highlight.scrollLeft = textarea.scrollLeft;
  };
  const syncScroll = () => {
    highlight.scrollTop = textarea.scrollTop;
    highlight.scrollLeft = textarea.scrollLeft;
  };

  // Typing repaints on the next frame: a pasted stack trace would otherwise rebuild the
  // whole highlight layer once per keystroke.
  let queued = false;
  const queuePaint = () => {
    if (queued) return;
    queued = true;
    const run = () => { queued = false; paint(); };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else run();
  };

  textarea.addEventListener("input", queuePaint);
  textarea.addEventListener("scroll", syncScroll);
  textarea.addEventListener("focus", paint);
  paint();
  watchCodeSurfaceDialog(textarea, paint);
  return true;
}

// A field's value is reset by whichever code opens its dialog, and assigning .value
// raises no `input` event -- so without this the previous text's highlight would stay
// ghosted behind a freshly emptied field, looking like real content. Watching the field's
// own dialog keeps this general instead of naming the dialogs that exist.
function watchCodeSurfaceDialog(textarea, paint) {
  if (typeof MutationObserver !== "function" || typeof textarea.closest !== "function") return;
  const overlay = textarea.closest(".modal-overlay");
  if (!overlay) return;
  const observer = new MutationObserver(() => {
    if (overlay.style.display === "none") return;
    paint();
  });
  observer.observe(overlay, { attributes: true, attributeFilter: ["style"] });
}

function initCodeSurfaces() {
  const fields = document.querySelectorAll(".modal-textarea-input");
  for (let i = 0; i < fields.length; i++) {
    buildCodeSurface(fields[i]);
  }
}
