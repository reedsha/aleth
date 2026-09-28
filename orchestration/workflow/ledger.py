"""The Living Behavioral Ledger: how a finished milestone is written back into the plan.

Three things happen when a Coder completes a task, and all three are deterministic plan
bookkeeping rather than model work:

* a short ``🟢 Behavioral Log:`` line is appended beneath the completed checkbox, so the
  plan carries *what was actually done* next to the claim that it was done -- the inline
  ledger the context slicer hands to the next task;
* the finished milestone is compressed into a single core fact in the ``## 🌍 Global State
  Summary``, so the standing context grows by decisions rather than by prose (the
  milestone wrap-up directive in the Architect prompt);
* both are pure functions over the plan mapping, so they are testable without a workflow,
  a model, or the disk.

The marker is written and read in two files (the compiler emits it, the parser reads it
back), so the prefix is defined once -- in ``tools.plan_parser``, beside the other markdown
syntax -- and imported here. A second spelling would mean the parser silently stopped
recognising what the compiler wrote, and the log would re-accumulate as an ordinary detail
note on every round-trip.
"""

from typing import Any, Mapping, Optional

from tools.plan_parser import BEHAVIORAL_LOG_PREFIX

# The heading the compressed core facts live under, when a plan has no summary yet.
STATE_SUMMARY_TITLE = "\U0001F30D Global State Summary"

# A wrap-up fact is one line of standing context, not a paragraph; the summary is carried
# into every prompt, so its length is a per-task cost.
MAX_FACT_CHARS = 160


def behavioral_log_line(message: str) -> str:
    """Renders one ledger entry exactly as the compiler writes it (unindented)."""
    return f"{BEHAVIORAL_LOG_PREFIX} {str(message or '').strip()}".rstrip()


def record_behavioral_log(entry: Mapping[str, Any], message: str) -> None:
    """Appends a ledger entry to a task or sub-step, in place.

    De-duplicated: a re-run of the same task must not stack identical entries, the same
    rule the plan's own detail notes follow.
    """
    text = str(message or "").strip()
    if not text:
        return
    log = entry.setdefault("behavioral_log", [])
    if text not in log:
        log.append(text)


def milestone_fact(task: Mapping[str, Any]) -> str:
    """The single core fact a finished milestone contributes to the standing context.

    Deliberately one line and built only from what the plan already carries: the task's
    tag (when it has one), its title, and where its deliverable went. Nothing here needs a
    model -- compression of a *deterministic* record is arithmetic, and spending tokens on
    it would defeat the point of keeping the standing context small.
    """
    title = str(task.get("title") or "").strip()
    tag = task.get("tag")
    files = [str(f) for f in (task.get("files") or []) if str(f).strip()]
    parts = []
    if tag:
        parts.append(f"[{tag}]")
    parts.append(title)
    fact = " ".join(p for p in parts if p).strip()
    if files:
        fact = f"{fact} \u2014 delivered `{files[0]}`"
        if len(files) > 1:
            fact += f" (+{len(files) - 1} more)"
    if len(fact) > MAX_FACT_CHARS:
        fact = fact[: MAX_FACT_CHARS - 1].rstrip() + "\u2026"
    return fact


def wrap_up_milestone(plan: Mapping[str, Any], task: Mapping[str, Any]) -> Optional[str]:
    """Compresses a finished milestone into a core fact, and returns the fact.

    Append-only and idempotent: the fact is added to ``state_summary.bullets`` only when it
    is not already there, so a milestone wrapped up twice does not read as two decisions.
    Returns ``None`` when the fact would be empty.
    """
    fact = milestone_fact(task)
    if not fact:
        return None

    summary = plan.get("state_summary")
    if not isinstance(summary, dict):
        summary = {"title": STATE_SUMMARY_TITLE, "bullets": []}
        plan["state_summary"] = summary
    bullets = summary.setdefault("bullets", [])
    if fact not in bullets:
        bullets.append(fact)
    return fact
