"""Markdown <-> plan-dict translation (compiled core).

The translation itself now lives in the `deepagents_core` Rust extension, which
walks a markdown-it-shaped token stream built on `pulldown-cmark` and returns the
strict plan dictionary as JSON. This module is the Python face of it: it supplies
the one thing the core deliberately does not own -- the tag vocabulary -- and keeps
the call signatures every other module already imports.

Pure translation: markdown text in, strict plan dictionary out (and back). This
module performs no file I/O and holds no state beyond reading the active plan
filename for its default argument, which makes it the easiest part of the plan
engine to test in isolation.

Four shapes of plan content are recognised, and only the first is a milestone:

    # Title                      the plan title
    ## 🌍 Global State Summary   standing facts, captured into `state_summary`
                                 instead of a task-less section
    - [ ] Task                   a milestone: gets a `task-N` id and a metric slot
      - [ ] Sub-step             folded into the parent task's `sub_steps`, never a
                                 task of its own, at any indent depth
      - detail bullet            a detail, or `files` when it declares deliverables

A milestone's checkbox mark *is* its status: ``[ ]`` pending, ``[-]`` in progress,
``[x]`` completed, and ``[!]`` failed -- a task the Coder wrote but that did not pass
the Architect's verification. The four marks round-trip through the compiler
unchanged.
"""

import functools
import json
from typing import Any, Dict, List, Set

import deepagents_core as _core

from tools.task_tags import CANONICAL, UI_KEYWORD_RE, UI_TAG
from tools.workspace import get_active_plan_filename
from tools.payloads import (
    PlanPayload,
    PlanStructureReport,
    VocabularyPayload,
    validated,
    validated_map,
    validated_str_list,
    validated_tasks,
)


# The inline Behavioral Ledger marker. Defined in the compiled core -- with the
# other markdown syntax that module owns -- because the compiler emits it and the
# parser reads it back; a second spelling here would make the parser stop
# recognising it and the log would re-accumulate as an ordinary detail note on
# every round-trip.
BEHAVIORAL_LOG_PREFIX: str = _core.BEHAVIORAL_LOG_PREFIX


@functools.lru_cache(maxsize=1)
def _vocabulary_json() -> str:
    """The tag vocabulary and UI keyword pattern, in the shape the core expects.

    Both halves live in ``tools.task_tags`` -- the single definition of the tag language,
    read by the plan tree and the delegation path alike, so the two cannot drift about what
    a tag is. That module imports nothing heavier than ``re``, so sourcing the UI keyword
    pattern from it rather than from ``orchestration.workflow.templates`` (whose package
    ``__init__`` pulls in the agent catalogue) is what keeps a cold plan parse in the
    milliseconds instead of seconds. Cached because every parse and compile call needs it
    and it never changes.
    """
    return json.dumps(_validated_vocabulary())


def _validated_vocabulary() -> Dict[str, Any]:
    """The vocabulary dictionary, checked against its contract before it is cached."""
    vocabulary: Dict[str, Any] = {
        "canonical": CANONICAL,
        "ui_tag": UI_TAG,
        "ui_keyword_pattern": UI_KEYWORD_RE.pattern,
    }
    return validated(VocabularyPayload, vocabulary)


def parse_markdown_to_plan_dict(
    content: str, filename: str = "PLAN.md", relaxed: bool = False
) -> Dict[str, Any]:
    """Parses Markdown plan files into a strict plan dictionary.

    Extracts headers, sections, tasks (with [x], [-], [ ] status and [UI] tag), the
    Global State Summary block, nested sub-steps, nested bullet details, and
    referenced deliverables. If ``relaxed=True``, extracts standard bulleted and
    numbered list items as pending tasks.
    """
    return validated(
        PlanPayload,
        json.loads(
            _core.parse_markdown_to_plan_dict(content, filename, relaxed, _vocabulary_json())
        ),
    )


def compile_plan_json_to_markdown(data: Dict[str, Any]) -> str:
    """Compiles a strict plan.json dictionary back into standardized Markdown.

    Ensures human readability, GitHub-Flavored Markdown checkboxes, and preservation
    of details.
    """
    validated(PlanPayload, data)
    return _core.compile_plan_json_to_markdown(json.dumps(data), _vocabulary_json())


def check_plan_structure(content: str) -> Dict[str, Any]:
    """Verdict on whether a document carries the plan AST the parser can read.

    A *shape* check, not a content check: it asks only for a title, at least one `##`
    section, and at least one `- [ ]` milestone, which is exactly what
    :func:`parse_markdown_to_plan_dict` needs to produce a task tree. Pure and
    read-only: the caller decides what, if anything, to rewrite.
    """
    return validated(PlanStructureReport, json.loads(_core.check_plan_structure(content)))


def parse_plan_tree(content: str) -> List[Dict[str, Any]]:
    """Backwards-compatible wrapper returning the steps list parsed from markdown.

    ZERO AI / LLM tokens used.
    """
    return validated_tasks(json.loads(
        _core.parse_plan_tree(content, get_active_plan_filename(), _vocabulary_json())
    ))


def explicit_tags(content: str) -> Dict[str, str]:
    """Every leading tag the markdown actually carries, as ``{clean title: tag}``.

    The parsed plan collapses an author's tag and one an engine inferred into the
    same fields. That is fine for rendering and not fine for re-tagging: a pass that
    re-derives tags would otherwise delete a tag a human wrote. This is the record
    of which tags a human wrote, and which one they wrote.
    """
    return validated_map(json.loads(_core.explicit_tags(content, _vocabulary_json())))


def explicit_ui_titles(content: str) -> Set[str]:
    """Titles carrying a *literal* ``[UI]`` tag."""
    return set(validated_str_list(json.loads(_core.explicit_ui_titles(content, _vocabulary_json()))))


def parse_deliverables(text: str) -> List[str]:
    """File paths declared by a `Files: a.py, b.py` style detail line, else []."""
    return list(validated_str_list(_core.parse_deliverables(text)))


def format_deliverables(files: List[str]) -> str:
    """Renders a files list as the detail line parse_deliverables() reads back."""
    return _core.format_deliverables([str(f) for f in files])
