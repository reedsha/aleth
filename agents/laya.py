"""Laya -- System 1 of the two-brain split: the zero-token decision engine.

System 2 (``agents/architect.py``) is the reasoning brain. Laya answers the questions
that are cheap, repetitive and pattern-shaped, so no model is ever spent on them:

* **Gatekeeper intent** -- is a free-form directive an administrative question the
  Architect answers itself, or an implementation request that needs a Coder? This is
  the decision ``custom_action`` used to make with nine bare ``in`` substring tests.
* **Coder routing** -- which Coder a task is delegated to.
* **Domain tagging** -- the vocabulary behind the plan's ``[UI]`` / ``[API]`` / ``[DB]``
  pills and the UI keyword the template selector already matches on.

Deliberately *not* owned here, because it is already zero-token, deterministic and
pinned by tests: resolving an ``[ACTION: ...]`` tag (``runner.resolve_intent``) and
choosing a generated deliverable (``templates.select``).

Two design rules:

1. This module must never import a model runtime. Ship the heuristic; a classifier
   swaps in behind the same ``classify`` seam later and no call site changes.
2. ``classify`` is pure and synchronous. It reads no clock, no disk and no global
   state, so it is trivially testable and safe to call on a hot path.

Scope note: there is no provider route wired up today -- every action path emits a
scripted event stream -- so Laya is the seam the real Coder loop will call, not a
token saving that exists right now.
"""

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

from orchestration.workflow.templates import UI_KEYWORD_RE

__all__ = [
    "INTENT_ADMIN",
    "INTENT_CODE",
    "CODER_DEEP",
    "CODER_STANDARD",
    "DOMAIN_UI",
    "DOMAIN_TESTS",
    "DOMAIN_DOCS",
    "DOMAIN_DB",
    "DOMAIN_API",
    "DOMAIN_CORE",
    "DOMAIN_GENERAL",
    "Verdict",
    "classify",
    "coder_for_domain",
    "inferred_ui",
    "route_coder",
]


# --- Verdict vocabulary ------------------------------------------------------

INTENT_ADMIN = "admin"
INTENT_CODE = "code"

CODER_DEEP = "coder-deep"
CODER_STANDARD = "coder-standard"

DOMAIN_UI = "UI"
DOMAIN_TESTS = "TESTS"
DOMAIN_DOCS = "DOCS"
DOMAIN_DB = "DB"
DOMAIN_API = "API"
DOMAIN_CORE = "CORE"
DOMAIN_GENERAL = "GENERAL"


# --- Evidence vocabularies ---------------------------------------------------

# Directives that ask the Architect to *look at* something rather than change it. The
# multi-word entries are here rather than in the interrogative list so they only fire as
# written: "how does" is a signal, a bare "how" is not. Word-boundary matched, because
# the bare substring test this replaces is what read "review the code change I want" as
# administrative purely on the strength of the letters in it.
_ADMIN_MARKERS = (
    "analyze", "analyse", "assess", "audit", "clarify", "compare", "describe",
    "evaluate", "explain", "overview", "plan", "recommend", "review", "roadmap",
    "status", "summarize", "summarise",
    "how does", "tell me", "walk me through", "what about", "what is",
)

# Verbs that ask for something to be built or changed.
_CODE_VERBS = (
    "add", "build", "bump", "change", "configure", "convert", "create", "debug",
    "delete", "deploy", "disable", "edit", "enable", "expose", "extract", "fix",
    "generate", "handle", "hook", "implement", "install", "integrate", "migrate",
    "modify", "move", "optimize", "optimise", "patch", "port", "refactor",
    "remove", "rename", "replace", "rewrite", "scaffold", "split", "update",
    "upgrade", "validate", "wire", "write",
)

# A directive that opens with one of these is a request, not a question.
_LEADING_INTERROGATIVES = (
    "are", "can", "could", "did", "do", "does", "how", "is", "should", "what",
    "when", "where", "which", "who", "whose", "why", "will", "would",
)

# Source extensions that make a directive about a *file*. "md" is deliberately absent:
# a markdown path is a document, not application code.
_SOURCE_EXTENSIONS = (
    "py", "pyi", "js", "mjs", "cjs", "jsx", "ts", "tsx", "css", "scss", "html",
    "htm", "json", "jsonl", "sql", "sh", "bash", "ps1", "yml", "yaml", "toml",
    "ini", "cfg", "java", "go", "rs", "rb", "php", "c", "h", "cpp", "hpp", "cs",
    "kt", "swift", "vue", "svelte",
)

# The plan file itself is never application code, whatever its extension looks like.
_PLAN_FILE_RE = re.compile(r"\b(?:plan|roadmap|backlog)\.(?:md|json|txt)\b", re.IGNORECASE)

# Naming the plan as an object is also administrative: "add a milestone to the plan" is
# a plan edit even though "add" is a code verb.
_PLAN_ARTIFACT_RE = re.compile(
    r"\b(?:plans?|roadmaps?|backlogs?|milestones?|status report)\b", re.IGNORECASE
)

_SOURCE_EXTENSION_ALTERNATION = "|".join(
    sorted(_SOURCE_EXTENSIONS, key=len, reverse=True)
)

_CODE_ARTIFACT_RE = re.compile(
    rf"[\w.-]+\.(?:{_SOURCE_EXTENSION_ALTERNATION})\b", re.IGNORECASE
)

_CODE_DECL_RE = re.compile(
    r"(?m)^\s*(?:def|class|import|from|function|const|let|var|async\s+def"
    r"|SELECT|INSERT|CREATE|ALTER|DROP)\b",
    re.IGNORECASE,
)

# Checked in order, so a title that mentions tests *and* an API is treated as a test task:
# the more specific working instruction wins.
_DOMAIN_PATTERNS = (
    (DOMAIN_TESTS, re.compile(
        r"\b(?:tests?|unittest|pytest|coverage|fixtures?|assertions?|mocks?|stubs?)\b",
        re.IGNORECASE)),
    (DOMAIN_DOCS, re.compile(
        r"\b(?:docs?|documentation|readme|docstrings?|changelog|comments?)\b",
        re.IGNORECASE)),
    (DOMAIN_DB, re.compile(
        r"\b(?:databases?|db|schemas?|migrations?|sql|queries|query|postgres|postgresql"
        r"|sqlite|mysql|orm|tables?|indexes|indices|constraints?)\b",
        re.IGNORECASE)),
    (DOMAIN_API, re.compile(
        r"\b(?:apis?|endpoints?|routes?|controllers?|servers?|handlers?|requests?"
        r"|responses?|rest|graphql|fastapi|flask|django|middleware|webhooks?)\b",
        re.IGNORECASE)),
    (DOMAIN_CORE, re.compile(
        r"\b(?:core|algorithms?|architectures?|engines?|parsers?|compilers?|optimizers?"
        r"|concurrency|threads?|threading|async|security|auth|authentication"
        r"|authorization|encryption|caches?|models?)\b",
        re.IGNORECASE)),
)


def _word_re(words: Tuple[str, ...]) -> "re.Pattern[str]":
    """One word-boundary alternation for a vocabulary.

    Longer entries are tried first so a multi-word marker is never shadowed by a
    single-word one that happens to be a prefix of it.
    """
    ordered = sorted(words, key=len, reverse=True)
    alternation = "|".join(re.escape(word) for word in ordered)
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


_ADMIN_RE = _word_re(_ADMIN_MARKERS)
_CODE_VERB_RE = _word_re(_CODE_VERBS)


def _hits(pattern: "re.Pattern[str]", lowered: str) -> List[str]:
    """Distinct vocabulary tokens present in the text, in order of appearance."""
    found: List[str] = []
    for match in pattern.finditer(lowered):
        token = match.group(0)
        if token not in found:
            found.append(token)
    return found


def _first_pos(pattern: "re.Pattern[str]", lowered: str) -> Optional[int]:
    match = pattern.search(lowered)
    return match.start() if match else None


def _is_question(lowered: str) -> bool:
    if lowered.endswith("?"):
        return True
    first = lowered.split(" ", 1)[0].strip(".,!:;")
    return first in _LEADING_INTERROGATIVES


def _has_code_artifact(raw: str) -> bool:
    if "```" in raw:
        return True
    if _CODE_ARTIFACT_RE.search(raw):
        return True
    return bool(_CODE_DECL_RE.search(raw))


def _domain_of(lowered: str, is_ui: bool, reasons: List[str]) -> str:
    if is_ui:
        reasons.append("task carries an explicit [UI] tag")
        return DOMAIN_UI
    ui_match = UI_KEYWORD_RE.search(lowered)
    if ui_match:
        reasons.append(f"UI keyword matched: '{ui_match.group(0)}'")
        return DOMAIN_UI
    for domain, pattern in _DOMAIN_PATTERNS:
        match = pattern.search(lowered)
        if match:
            reasons.append(f"{domain.lower()} vocabulary matched: '{match.group(0)}'")
            return domain
    return DOMAIN_GENERAL


@dataclass(frozen=True)
class Verdict:
    """One decision plus the evidence that produced it.

    ``confidence`` is a coarse band between 0.4 and 0.9, not a calibrated probability.
    It exists so a single threshold can be applied at the call site once a model sits
    behind ``classify``; today nothing gates on it and it is informational only.

    ``coder`` is the *plan task* routing decision (``route_coder``). A free-form
    directive has no task title, so the Gatekeeper branches should use
    ``coder_for_domain(verdict.domain)`` instead.
    """

    intent: str
    domain: str
    coder: str
    confidence: float
    reasons: Tuple[str, ...] = ()


# Domains whose work is boilerplate rather than reasoning. These are the only two a
# junior Coder is the right choice for, per the Architect's routing directive.
_STANDARD_CODER_DOMAINS = (DOMAIN_TESTS, DOMAIN_DOCS)


def route_coder(text: str, is_ui: bool = False) -> str:
    """Picks the Coder for a *plan task*, reproducing today's rule exactly.

    ``next_step_action`` routes deep iff the task is tagged UI or its title contains
    "core" -- a bare substring test, so "score" and "encore" route deep as well. That
    quirk is preserved on purpose: wiring Laya in must not move a single existing
    delegation, and both ends of it are pinned by the characterization suite.
    """
    if is_ui or "core" in (text or "").lower():
        return CODER_DEEP
    return CODER_STANDARD


def coder_for_domain(domain: str) -> str:
    """Picks the Coder for a *free-form directive* from its domain alone.

    The ``custom`` branch has no task title to inspect, so ``route_coder`` cannot serve
    it. Today ``custom`` hardcodes the deep Coder, which is why this returns deep for
    everything except the two boilerplate domains -- that keeps the pinned custom
    directive ("add a login endpoint") on ``coder-deep`` while letting a genuine
    "write the tests for this" directive reach the standard Coder.

    ``fix_bug`` is deliberately not routed through this: a diagnosis has to reason about
    a root cause, so it stays deep unconditionally.
    """
    return CODER_STANDARD if domain in _STANDARD_CODER_DOMAINS else CODER_DEEP


# The ``[UI]`` marker is metadata, never prose. A task that *describes* the tag --
# "Wire zero-token `[UI]` task tagging into the parser" -- would otherwise be tagged on
# the strength of the literal it happens to mention. Standalone tags are consumed by the
# parser before this predicate runs; this only neutralises the ones left in prose or in
# backticks. Neutralised rather than deleted so the surrounding words stay apart:
# "the [UI] tag" must not read as "the tag".
_UI_TAG_LITERAL_RE = re.compile(r"\[ui\]", re.IGNORECASE)


def inferred_ui(title: str, is_ui: bool = False) -> bool:
    """Whether a task should carry the ``[UI]`` tag in the plan tree.

    An explicit tag always wins; otherwise the vocabulary shared with
    ``templates.select`` decides, so a task can never be tagged in the tree yet
    rendered as a plain module on the delegation path.

    Every other part of a title counts, including backticked paths: a task that names
    ``ui/js/sidebar.js`` is UI work however it is punctuated.
    """
    if is_ui:
        return True
    prose = _UI_TAG_LITERAL_RE.sub(" ", title or "")
    return bool(UI_KEYWORD_RE.search(prose.lower()))


def classify(text: str, context: Optional[Mapping[str, Any]] = None) -> Verdict:
    """Decides what a directive is asking for, and who should handle it.

    ``context`` is optional and currently reads only ``is_ui``, so the same function
    serves a free-form Gatekeeper message and a plan task's title. The rules are
    ordered by how decisive their evidence is; the first one that fires wins and its
    reason is recorded on the verdict.
    """
    raw = (text or "").strip()
    lowered = raw.lower()
    is_ui = bool((context or {}).get("is_ui", False))

    reasons: List[str] = []
    domain = _domain_of(lowered, is_ui, reasons)

    code_hits = _hits(_CODE_VERB_RE, lowered)
    admin_hits = _hits(_ADMIN_RE, lowered)
    lead_match = _CODE_VERB_RE.search(lowered)
    leads_with_verb = lead_match is not None and lead_match.start() == 0
    lead_token = lead_match.group(0) if lead_match else ""

    if _PLAN_FILE_RE.search(lowered):
        intent, confidence = INTENT_ADMIN, 0.9
        reasons.append("names the plan file itself rather than application code")
    elif _PLAN_ARTIFACT_RE.search(lowered) and not _has_code_artifact(raw):
        intent, confidence = INTENT_ADMIN, 0.85
        reasons.append("names a plan document rather than application code")
    elif _has_code_artifact(raw) and code_hits:
        intent, confidence = INTENT_CODE, 0.9
        reasons.append("names a code artifact and a code action")
    elif leads_with_verb:
        intent, confidence = INTENT_CODE, 0.85
        reasons.append(f"opens with the code action '{lead_token}'")
    elif code_hits and not admin_hits:
        intent, confidence = INTENT_CODE, 0.8
        reasons.append(f"code action '{code_hits[0]}' with no analytical marker")
    elif _is_question(lowered):
        intent, confidence = INTENT_ADMIN, 0.8
        reasons.append("phrased as a question")
    elif admin_hits and not code_hits:
        intent, confidence = INTENT_ADMIN, 0.8
        reasons.append(f"analytical marker '{admin_hits[0]}' with no code action")
    elif admin_hits and code_hits:
        admin_pos = _first_pos(_ADMIN_RE, lowered)
        code_pos = _first_pos(_CODE_VERB_RE, lowered)
        if code_pos is not None and (admin_pos is None or code_pos < admin_pos):
            intent, confidence = INTENT_CODE, 0.55
            reasons.append("code action precedes the analytical marker")
        else:
            intent, confidence = INTENT_ADMIN, 0.55
            reasons.append("analytical marker precedes the code action")
    else:
        intent, confidence = INTENT_CODE, 0.4
        reasons.append("no decisive marker; defaulting to the delegation path")

    return Verdict(
        intent=intent,
        domain=domain,
        coder=route_coder(raw, is_ui=is_ui),
        confidence=confidence,
        reasons=tuple(reasons),
    )
