#!/usr/bin/env python3
"""Inline the frontend modules into ui/index.html.

The frontend is authored as ordered classic-script modules in ``ui/js/``. Loading
them through eleven ``<script src>`` tags made startup depend on eleven separate
``file://`` subresource fetches, and WebView2 fails individual ones intermittently.
A lost fetch left the window fully rendered but inert -- no click handlers at all --
which is indistinguishable from a working app because the packaged build runs with
``debug=False``.

Inlining keeps every module as its own ``<script>`` element, so they still share one
global lexical scope and a throw in one still cannot abort the next. The only thing
that changes is that there is no subresource fetch left to fail.

``ui/js/`` remains the source of truth. Regenerate after editing it::

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

# Load order matters. wire.js must stay last: it registers the DOMContentLoaded
# bootstrap, which calls into every module above it.
MODULES = [
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
    "wire.js",
]

BEGIN = "  <!-- BEGIN UI BUNDLE - generated from ui/js/*.js by tools/build_ui_bundle.py -->"
END = "  <!-- END UI BUNDLE -->"

# The existing bundle region, so the generator is idempotent.
BUNDLE_RE = re.compile(re.escape(BEGIN) + r"[\s\S]*?" + re.escape(END) + r"\n?")

# The <script src> block this replaced, so the generator also runs on a checkout
# that predates the inline bundle.
LEGACY_RE = re.compile(
    r'  <!-- Frontend logic is split[\s\S]*?<script src="js/wire\.js"></script>\n'
)

# An inline script ends at the first "</script", so module sources must not contain
# one. Guarded rather than assumed: a silent mismatch would corrupt the document.
FORBIDDEN = "</script"


def read_module(name: str) -> str:
    source = (JS_DIR / name).read_text(encoding="utf-8")
    if FORBIDDEN in source:
        raise SystemExit(f"ui/js/{name} contains {FORBIDDEN!r}; it cannot be inlined")
    return source.strip("\n")


def render_bundle() -> str:
    parts = [BEGIN]
    for name in MODULES:
        parts.append("  <script>")
        parts.append(read_module(name))
        parts.append("  </script>")
    parts.append(END)
    return "\n".join(parts) + "\n"


def build(html: str) -> str:
    block = render_bundle()
    if BUNDLE_RE.search(html):
        return BUNDLE_RE.sub(lambda _match: block, html, count=1)
    match = LEGACY_RE.search(html)
    if not match:
        raise SystemExit(
            "ui/index.html has neither a UI bundle marker nor the legacy "
            "<script src> block; refusing to guess where the bundle belongs"
        )
    return html[: match.start()] + block + html[match.end() :]


def main(argv: list[str]) -> int:
    html = INDEX.read_text(encoding="utf-8")
    updated = build(html)

    if "--check" in argv:
        if updated != html:
            print("ui/index.html is stale: run `python tools/build_ui_bundle.py`")
            return 1
        print(f"ui/index.html inline bundle is up to date ({len(MODULES)} modules)")
        return 0

    if updated == html:
        print("ui/index.html is already up to date")
        return 0

    INDEX.write_text(updated, encoding="utf-8", newline="\n")
    print(f"inlined {len(MODULES)} modules into ui/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
