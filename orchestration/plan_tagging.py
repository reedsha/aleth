"""Offline System 1 tagging: decide every task's ``[UI]`` flag once, and persist it.

The ``[UI]`` tag is not cosmetic. It is the ``[UI] `` prefix in the plan markdown, the
``is_ui`` field in ``plan.json``, and the input to three separate decisions: the domain
pill the plan tree draws, ``templates.select`` (which deliverable a task compiles to) and
``route_coder`` (the deep or the standard Coder). One wrong tag changes what gets built.

The four-keyword word list in ``agents/laya.py`` is the wrong engine for deciding it: on a
hand-labelled set it scores 4/10 on domain, missing every UI task that does not literally
say "ui", where the Laya checkpoint scores 9/10. But the checkpoint costs about a second
per title on CPU. So the two are separated by *when* they run rather than by replacing
one with the other:

* ``tools/plan_parser.py`` keeps the word list, because it runs on every render that
  notices a newer ``PLAN.md`` and has to stay instant;
* this module is the deliberate pass that spends that second per title and writes the
  answer back, so every later render reads a good tag for free.

Two rules make it safe to run over a plan a human authored:

* an explicit ``[UI]`` is never cleared -- a re-tagging pass must not drop a fact someone
  asserted on purpose, so only *inferred* tags are re-derived;
* it defaults to ``dry_run`` behaviour at the caller's discretion, because it rewrites the
  text of that file, and that should be inspectable before it is trusted.

Nothing here imports ``agents`` at module scope: ``orchestration/__init__`` imports this
module, and ``agents.laya`` imports ``orchestration.workflow.templates``, so a module-level
agent import here would close an import cycle.
"""

from typing import Any, Callable, Dict, List, Optional, Set

from tools.plan_parser import explicit_ui_titles
from tools.plan_state import load_plan_state, save_plan_state
from tools.task_tags import UI_TAG

DOMAIN_UI = "UI"

# The engine answers with a coarse domain, and only these map onto the plan's tag
# vocabulary without stretching it: the checkpoint scores 9/10 over seven classes, where
# asking a 421M model to choose among the vocabulary's fifty-seven tags would not hold
# up. "general" deliberately maps to nothing -- untagged is a real answer, and a wrong
# tag is worse than an absent one, because the tree would render it as a claim.
_TAG_BY_DOMAIN = {
    "UI": "UI",
    "API": "API",
    "DB": "DB",
    "TESTS": "TEST",
    "DOCS": "DOCS",
    "CORE": "BIZ",
}


def _entries(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A task and its sub-steps. Each carries its own tag, so each is decided separately."""
    return [task] + [s for s in (task.get("sub_steps") or []) if isinstance(s, dict)]


def _plan_markdown() -> str:
    """The active plan's markdown, or ``""`` when it cannot be read."""
    import os

    from tools.workspace import get_active_plan_filename, get_project_dir

    path = os.path.join(get_project_dir(), get_active_plan_filename())
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def retag_plan(
    dry_run: bool = False,
    classify: Optional[Callable[[str], Any]] = None,
    explicit: Optional[Set[str]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Re-derives every *inferred* ``[UI]`` tag with the System 1 engine.

    ``classify`` defaults to the ``agents.laya_model`` seam, so ``LAYA_BACKEND`` decides
    whether the checkpoint or the word list answers, and a checkpoint that is missing or
    fails falls back to the word list rather than taking the pass down.

    ``explicit`` is the set of titles carrying a literal ``[UI]`` in the markdown; those
    are held True whatever the engine says. It is read from the active plan by default.

    ``on_progress`` is called as ``(done, total, title)`` just before each title is
    decided, on the deciding thread. The pass costs on the order of a second per title, so
    a caller needs to be able to show that it is still moving rather than appearing hung.

    Returns the counts, the individual changes, and which engine answered -- enough to
    show the pass's work without re-reading the plan.
    """
    from agents import laya_model

    engine = classify or laya_model.classify
    if explicit is None:
        explicit = explicit_ui_titles(_plan_markdown())

    plan = load_plan_state()
    entries: List[Dict[str, Any]] = []
    for section in plan.get("sections", []):
        for task in section.get("tasks", []):
            entries.extend(_entries(task))

    total = 0
    ui_before = 0
    ui_after = 0
    kept_explicit = 0
    changed: List[Dict[str, str]] = []

    for entry in entries:
        title = (entry.get("title") or "").strip()
        total += 1
        if on_progress is not None:
            on_progress(total, len(entries), title)
        was = bool(entry.get("is_ui"))
        ui_before += was

        if title in explicit:
            now = True
            kept_explicit += 1
        else:
            verdict = engine(title)
            domain = str(getattr(verdict, "domain", "")).upper()
            now = domain == DOMAIN_UI
            tag = _TAG_BY_DOMAIN.get(domain)
            if tag:
                entry["tag"] = tag
            else:
                entry.pop("tag", None)

        ui_after += now
        if now != was:
            changed.append(
                {
                    "id": entry.get("id") or "",
                    "title": title,
                    "was": "ui" if was else "plain",
                    "now": "ui" if now else "plain",
                }
            )
        entry["is_ui"] = now

    if not dry_run and changed:
        save_plan_state(plan)

    return {
        "total": total,
        "ui_before": ui_before,
        "ui_after": ui_after,
        "kept_explicit": kept_explicit,
        "changed": changed,
        "engine": laya_model.active_engine(),
        "dry_run": dry_run,
        "written": bool(changed) and not dry_run,
    }
