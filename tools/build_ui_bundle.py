#!/usr/bin/env python3
"""Inline the frontend modules and stylesheets into ui/index.html.

The frontend is authored as ordered classic-script modules in ``ui/js/`` and as
ordered stylesheets in ``ui/css/``. Loading them through separate ``<script src>``
/ ``<link>`` tags made startup depend on a separate HTTP subresource fetch per
file, and WebView2 -- which pywebview drives -- fails individual ones
intermittently. A lost module fetch left the window fully rendered but inert (no
click handlers at all), which is indistinguishable from a working app because the
packaged build runs with ``debug=False``.

Inlining every file keeps each JS module as its own ``<script>`` element -- so a
throw in one still cannot abort the next -- and each stylesheet as its own
``<style>`` element. Declaration order is preserved exactly, so the CSS cascade is
unchanged. The only difference from the original is that there is no subresource
fetch left to fail.

``ui/js/`` and ``ui/css/`` are the sources of truth. The former ``ui/styles.css``
was split into ``ui/css/*.css`` and is no longer referenced. Regenerate after
editing either directory::

    python tools/build_ui_bundle.py

``--check`` reports staleness instead of writing and is used by the test suite.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "ui" / "index.html"
JS_DIR = ROOT / "ui" / "js"
CSS_DIR = ROOT / "ui" / "css"

# JS load order matters. wire.js must stay last: it registers the DOMContentLoaded
# bootstrap, which calls into every module above it.
JS_MODULES = [
    "state.js",
    "dom.js",
    "bootstrap.js",
    "plan-tree.js",
    "actions.js",
    "plan-modals.js",
    "agents.js",
    "workspace.js",
    "agent-events.js",
    "visuals.js",
    "dock.js",
    "workbench.js",
    "code-surface.js",
    "console.js",
    "diff-pane.js",
    "preview.js",
    "sidebar.js",
    "env.js",
    "wire.js",
]

# CSS cascade order matters for rules of equal specificity, so this list preserves
# the order the sections had in the original single stylesheet.
CSS_MODULES = [
    "base.css",
    "sidebar.css",
    "stage.css",
    "actions.css",
    "plan-tree.css",
    "modals.css",
    "tiered-prompt-editor.css",
    "ui-vision.css",
    "audit-modal.css",
    "rollback-modal.css",
    "dock.css",
    "workbench.css",
    "code-surface.css",
    "console.css",
    "diff-pane.css",
    "preview.css",
    "sidebar-panels.css",
]

JS_BEGIN = "  <!-- BEGIN UI BUNDLE - generated from ui/js/*.js by tools/build_ui_bundle.py -->"
JS_END = "  <!-- END UI BUNDLE -->"
CSS_BEGIN = "  <!-- BEGIN STYLE BUNDLE - generated from ui/css/*.css by tools/build_ui_bundle.py -->"
CSS_END = "  <!-- END STYLE BUNDLE -->"

# The existing bundle regions, so the generator is idempotent.
JS_BUNDLE_RE = re.compile(re.escape(JS_BEGIN) + r"[\s\S]*?" + re.escape(JS_END) + r"\n?")
CSS_BUNDLE_RE = re.compile(re.escape(CSS_BEGIN) + r"[\s\S]*?" + re.escape(CSS_END) + r"\n?")

# The legacy tags this replaced, so the generator also runs on a checkout that
# predates the inline bundles.
JS_LEGACY_RE = re.compile(
    r'  <!-- Frontend logic is split[\s\S]*?<script src="js/wire\.js"></script>\n'
)
CSS_LEGACY_RE = re.compile(r'  <link rel="stylesheet" href="styles\.css">\n')

# An inline script ends at the first "</script" and an inline style at the first
# "</style", so sources must not contain those. Guarded rather than assumed: a
# silent mismatch would corrupt the document.
JS_FORBIDDEN = "</script"
CSS_FORBIDDEN = "</style"


def read_source(directory: Path, name: str, forbidden: str) -> str:
    source = (directory / name).read_text(encoding="utf-8")
    if forbidden in source.lower():
        raise SystemExit(f"{directory.name}/{name} contains {forbidden!r}; it cannot be inlined")
    return source.strip("\n")


def render_js_bundle() -> str:
    parts = [JS_BEGIN]
    for name in JS_MODULES:
        parts.append("  <script>")
        parts.append(read_source(JS_DIR, name, JS_FORBIDDEN))
        parts.append("  </script>")
    parts.append(JS_END)
    return "\n".join(parts) + "\n"


def render_css_bundle() -> str:
    parts = [CSS_BEGIN]
    for name in CSS_MODULES:
        parts.append("  <style>")
        parts.append(read_source(CSS_DIR, name, CSS_FORBIDDEN))
        parts.append("  </style>")
    parts.append(CSS_END)
    return "\n".join(parts) + "\n"


def replace_region(html: str, bundle_re: re.Pattern[str], legacy_re: re.Pattern[str],
                   block: str, description: str) -> str:
    if bundle_re.search(html):
        return bundle_re.sub(lambda _match: block, html, count=1)
    match = legacy_re.search(html)
    if not match:
        raise SystemExit(
            f"ui/index.html has neither a {description} marker nor the legacy markup "
            f"it replaced; refusing to guess where the block belongs"
        )
    return html[: match.start()] + block + html[match.end() :]


def build(html: str) -> str:
    html = replace_region(html, CSS_BUNDLE_RE, CSS_LEGACY_RE, render_css_bundle(),
                          "STYLE BUNDLE")
    return replace_region(html, JS_BUNDLE_RE, JS_LEGACY_RE, render_js_bundle(),
                          "UI BUNDLE")


def main(argv: list[str]) -> int:
    html = INDEX.read_text(encoding="utf-8")
    updated = build(html)

    if "--check" in argv:
        if updated != html:
            print("ui/index.html is stale: run `python tools/build_ui_bundle.py`")
            return 1
        print(
            f"ui/index.html inline bundles are up to date "
            f"({len(JS_MODULES)} modules, {len(CSS_MODULES)} stylesheets)"
        )
        return 0

    if updated == html:
        print("ui/index.html is already up to date")
        return 0

    INDEX.write_text(updated, encoding="utf-8", newline="\n")
    print(f"inlined {len(JS_MODULES)} modules and {len(CSS_MODULES)} stylesheets into ui/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
