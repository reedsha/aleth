"""The one definition of the project's task-tag language.

A tag such as ``[AUTH]`` is written and read in three unrelated places: the plan parser
that strips it off a title, the compiler that writes it back out, and the UI that
renders and offers it. When each of those owns its own list the three drift -- the UI
offers a tag the parser no longer recognises, or one accepts a spelling another never
emits. Defining the vocabulary here means those components can disagree only with a
deliberate edit to this file, never with each other.

The vocabulary is flat: one ordered list of tags, each with its own human label and its
own description. There are no grouping categories any more -- a tag *is* its label's
category, so ``category_of`` answers with the label itself.

The legacy ``UI`` spelling is kept as an alias of the current ``FE`` tag, and it is not
optional. The plan on disk carries ``[UI]`` tokens written by an earlier vocabulary
(``my_project_workspace/PLAN.md`` has twenty-two of them), so ``[UI]`` must still parse.
If ``UI`` were dropped, ``split_tag("[UI] Some title")`` would stop recognising the
token, leave it in the title as literal text, and the compiler would write it back
alongside the inferred tag -- compounding the stray token on every save and quietly
corrupting the user's plan. ``UI`` therefore remains a recognised alias that
canonicalises to ``FE``.
"""

import re
from typing import Dict, List, Optional, Tuple

# Authored as an ordered literal, not built from a set: the mapping doubles as the
# display order the UI presents the tags in. (tag, human label, description)
ENTRIES: List[Tuple[str, str, str]] = [
    ("FE", "Frontend", "UI components, UX flows, CSS, styling, web/mobile views"),
    ("BE", "Backend", "Core domain logic, business rules, server processing"),
    ("API", "Interfaces", "REST, GraphQL, gRPC endpoints, routes, API specs"),
    ("DB", "Database", "Schemas, SQL queries, migrations, ORMs"),
    ("AUTH", "Identity", "Authentication, OAuth, JWT, permissions, RBAC"),
    ("INFRA", "Infrastructure", "Cloud resources, Terraform, networking, DNS, load balancers"),
    ("DEVOPS", "Delivery", "CI/CD pipelines, Docker, Kubernetes, release scripts"),
    ("TEST", "Quality", "Unit, integration, E2E tests, mocks, test runners"),
    ("SEC", "Security", "Vulnerability fixes, security audits, hardening, compliance"),
    ("OBS", "Observability", "Logging, metrics, tracing, alerts, dashboards"),
    ("PERF", "Optimization", "Query tuning, caching (Redis), memory leaks, bundle size"),
    ("ASYNC", "Event Processing", "Background queues, pub/sub, WebSockets, background workers"),
    ("INTEG", "External Services", "Third-party SDKs, external API wrappers, webhooks"),
    ("CONFIG", "System Setup", "Env variables, feature flags, secrets management"),
    ("DATA", "Data Systems", "Data pipelines, ETL jobs, analytics, batch tasks"),
    ("AI", "AI & ML", "Prompts, LLM workflows, vector databases, model integrations"),
    ("ARCH", "Design", "System architecture, ADRs, high-level technical decisions"),
    ("REFACTOR", "Code Health", "Technical debt resolution, code cleanup, restructuring"),
    ("BUG", "Defect Fixes", "Bug resolution, edge-case fixes, error handling"),
    ("HOTFIX", "Production Patch", "Immediate production patches, critical fixes"),
    ("SPIKE", "Research", "Exploratory code, feasibility checks, POCs"),
    ("DOCS", "Documentation", "READMEs, dev guides, inline code documentation"),
    ("DX", "Tooling", "Linters, compilers, bundlers, local dev tools"),
    ("DEPS", "Packages", "Dependency upgrades, lockfile management, version bumps"),
    ("I18N", "Localization", "Multi-language support, translation keys, formatting"),
]

ORDER: List[str] = [tag for tag, _, _ in ENTRIES]

TAGS: Dict[str, str] = {tag: description for tag, _, description in ENTRIES}

LABELS: Dict[str, str] = {tag: label for tag, label, _ in ENTRIES}

CATEGORIES: List[str] = [LABELS[tag] for tag in ORDER]

# The legacy spelling, kept so a plan written under the old vocabulary still parses.
# Dropping it would leave ``[UI]`` tokens sitting in titles as literal text, compounding
# on every save.
ALIASES: Dict[str, str] = {
    # Tags this project has already WRITTEN into plans. A rename must keep the old
    # spelling readable: the parser would otherwise stop recognising the token and leave
    # it in the title as literal text, compounding on every save. BIZ is here for the same
    # reason as UI -- an earlier pass wrote 22 [BIZ] prefixes into this project's plan
    # before the vocabulary was replaced.
    "UI": "FE",
    "BIZ": "BE",
}

# Tags are matched case-insensitively, so every lookup goes through this upper-cased
# index and comes back out carrying the spelling this file authored. The legacy aliases
# are indexed too, which is what makes ``[ui]`` canonicalise to ``FE``.
CANONICAL: Dict[str, str] = {tag.upper(): tag for tag in TAGS}
CANONICAL.update({alias.upper(): canonical for alias, canonical in ALIASES.items()})

_LABEL_BY_TAG: Dict[str, str] = {tag.upper(): label for tag, label, _ in ENTRIES}

UI_TAG: str = "FE"

# The explicit "no tag" answer. A classifier must be able to say *nothing fits*: without
# this option a choice over the vocabulary has to pick one, manufacturing a domain for work
# that has none -- and a wrong tag is worse than an absent one, because the tree renders it
# as a claim about the task.
NOTHING: str = "none"

# A leading tag is only a tag when it is bracketed at position zero and introduces the
# title; anything later is prose that merely mentions the tag.
_LEADING_TAG_RE = re.compile(r"^\[([^\[\]]+)\](?:[ \t]+(.*))?$")


def is_tag(name: str) -> bool:
    """Whether ``name`` is a tag in the vocabulary. Case-insensitive.

    Legacy aliases such as ``UI`` count: they are recognised spellings that canonicalise
    to a current tag.
    """
    return name.upper() in CANONICAL


def category_of(tag: str) -> Optional[str]:
    """The label a tag belongs to, or None.

    The vocabulary is flat, so a tag's category is its own label. An alias answers with
    the label of the tag it canonicalises to.
    """
    canonical = CANONICAL.get(tag.upper())
    if canonical is None:
        return None
    return _LABEL_BY_TAG.get(canonical.upper())


def tag_prefix(tag: str) -> str:
    """The markdown prefix a compiler writes for a tag, e.g. "[FE] " with a trailing space."""
    return f"[{CANONICAL.get(tag.upper(), tag)}] "


def split_tag(title: str) -> Tuple[str, Optional[str]]:
    """Splits a leading bracketed tag off a title.

    Returns (clean title, canonical tag) or (title unchanged, None) when there is no
    leading tag *in the vocabulary*. Only a tag at the very start counts: prose that
    merely mentions one, or a bracketed token that is not in the vocabulary such as
    "[WIP]", must be left in the title untouched.

    Case-insensitive, and the returned tag is the canonical spelling -- so the legacy
    ``[UI]`` comes back as ``"FE"`` rather than being left in the title.
    """
    match = _LEADING_TAG_RE.match(title)
    if not match:
        return title, None
    canonical = CANONICAL.get(match.group(1).strip().upper())
    if canonical is None:
        return title, None
    return (match.group(2) or "").strip(), canonical
