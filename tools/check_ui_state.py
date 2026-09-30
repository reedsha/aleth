"""Check the frontend's shared-state invariant (dev-only; not imported by the app).

The rule, which `ui/js/store.js` documents and this tool enforces:

    a `state` or `DOM` field may be written from **at most one** module.

The frontend's shared state used to be one global object that any of 21 classic scripts
could reach into, so answering "who changes this field?" meant reading all of them. Since
the ES-module migration a module can only touch what it imports, and the fields that
genuinely cross a module boundary are written through a named store operation instead.

Two facts make the rule sufficient rather than decorative:

* `DOM` is populated by `initDOMElements` in `dom.js` and read everywhere else, so it has
  exactly one writer by construction.
* Every other field has a single writer today. A second writer is the moment a field
  becomes shared mutable state, and that is the moment it should move behind a store
  operation -- which is what this tool is here to notice.

Usage::

    ./venv/Scripts/python.exe tools/check_ui_state.py        # exits non-zero on violation
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Set

UI_JS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "js")

# Every shape a write can take: `=`, `+=`, `-=`, `++`, `--`. A read is not a write and is
# deliberately not matched -- reads have no invariant to protect.
_WRITE = re.compile(r"\b(state|DOM)\.([A-Za-z_$][\w$]*)\s*(?:=[^=]|\+=|-=|\+\+|--)")

# The file allowed to hold the named operations for a shared field.
STORE_MODULE = "store.js"


def _strip_comments(source: str) -> str:
    """Comments removed, so a field named in prose is not read as a write."""
    out: List[str] = []
    in_block = False
    for line in source.splitlines():
        kept: List[str] = []
        index = 0
        while index < len(line):
            if in_block:
                end = line.find("*/", index)
                if end == -1:
                    index = len(line)
                else:
                    in_block = False
                    index = end + 2
                continue
            if line.startswith("//", index):
                break
            if line.startswith("/*", index):
                in_block = True
                index += 2
                continue
            kept.append(line[index])
            index += 1
        out.append("".join(kept))
    return "\n".join(out)


def writers_by_field() -> Dict[str, Set[str]]:
    """``{"state.foo": {"bar.js"}, ...}`` -- every module that writes each field."""
    writers: Dict[str, Set[str]] = {}
    for name in sorted(os.listdir(UI_JS_DIR)):
        if not name.endswith(".js"):
            continue
        with open(os.path.join(UI_JS_DIR, name), encoding="utf-8") as handle:
            source = _strip_comments(handle.read())
        for match in _WRITE.finditer(source):
            field = f"{match.group(1)}.{match.group(2)}"
            writers.setdefault(field, set()).add(name)
    return writers


def violations(writers: Dict[str, Set[str]]) -> List[str]:
    """Fields written from more than one module, each with the modules that write them."""
    return [
        f"{field}: {', '.join(sorted(modules))}"
        for field, modules in sorted(writers.items())
        if len(modules) > 1
    ]


def main() -> int:
    writers = writers_by_field()
    shared = violations(writers)

    if shared:
        print("Shared-state invariant violated: a field is written from more than one module.")
        print(f"Move each write behind a named operation in ui/js/{STORE_MODULE}.\n")
        for line in shared:
            print(f"  {line}")
        return 1

    print(
        f"ui/js: {len(writers)} written fields, every one with a single writer "
        f"(no field needs a store operation beyond the ones already there)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
