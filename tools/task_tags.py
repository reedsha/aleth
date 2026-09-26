"""The one definition of the project's task-tag language.

A tag such as ``[AUTH]`` is written and read in three unrelated places: the plan parser
that strips it off a title, the compiler that writes it back out, and the UI that
renders and offers it. When each of those owns its own list the three drift -- the UI
offers a tag the parser no longer recognises, or one accepts a spelling another never
emits. Defining the vocabulary here means those components can disagree only with a
deliberate edit to this file, never with each other.
"""

import re
from typing import Dict, List, Optional, Tuple

# Authored as an ordered literal, not built from a set: the mapping doubles as the
# display order the UI presents the categories and their tags in.
VOCABULARY: Dict[str, List[Tuple[str, str]]] = {
    "Frontend & User Experience": [
        ("UI", "User Interface components, layouts, visual updates"),
        ("UX", "User experience improvements, interaction flows, usability"),
        ("CSS", "Styling, Tailwind, Sass, design system tokens"),
        ("A11Y", "Accessibility compliance (WCAG, ARIA labels, screen readers)"),
        ("STATE", "Frontend state management (Redux, Zustand, Pinia, React Query)"),
        ("I18N", "Internationalization, localization, translation keys"),
        ("ANIM", "Animations, micro-interactions, canvas/WebGL work"),
    ],
    "Backend & Business Logic": [
        ("API", "REST/GraphQL/gRPC endpoints, routing, controllers"),
        ("AUTH", "Authentication, authorization, OAuth, JWT, permissions"),
        ("BIZ", "Core domain/business logic, business rules, workflows"),
        ("QUEUE", "Background jobs, message brokers (RabbitMQ, Kafka, BullMQ)"),
        ("CACHE", "Caching layers (Redis, Memcached, in-memory strategies)"),
        ("WS", "WebSockets, WebRTC, real-time event streaming"),
        ("SDK", "Internal or external API client/wrapper libraries"),
    ],
    "Databases & Persistence": [
        ("DB", "Database modifications, queries, indexes"),
        ("MIGR", "Database migrations, schema versioning"),
        ("SCHEMA", "Data modeling, DTOs, database definitions"),
        ("QUERY", "Query optimization, SQL tuning, indexing fixes"),
        ("ETL", "Data pipelines, batch processing, data transformations"),
        ("ORM", "Object-Relational Mapping configurations (Prisma, TypeORM)"),
    ],
    "Infrastructure, Cloud & DevOps": [
        ("INFRA", "Infrastructure as Code (Terraform, CloudFormation, CDK)"),
        ("CI/CD", "Pipeline configurations (GitHub Actions, GitLab CI, Jenkins)"),
        ("DOCKER", "Containerization, Dockerfiles, Docker Compose"),
        ("K8S", "Kubernetes manifests, Helm charts, cluster setup"),
        ("ENV", "Environment variables, config updates, secrets management"),
        ("DEPL", "Deployment setups, blue-green strategies, release scripts"),
        ("NET", "Networking, DNS, load balancers, CDNs, ingress"),
    ],
    "Quality Assurance & Testing": [
        ("TEST", "General test suite changes"),
        ("UNIT", "Unit testing (Jest, Vitest, PyTest)"),
        ("INTEG", "Integration tests between modules or services"),
        ("E2E", "End-to-End testing (Playwright, Cypress)"),
        ("LOAD", "Load testing, stress testing, performance benchmarking"),
        ("MOCK", "Test data generation, mocks, stubs, fixtures"),
    ],
    "Security, Compliance & Observability": [
        ("SEC", "Vulnerability patching, security hardening, dependency fixes"),
        ("AUDIT", "Compliance requirements (GDPR, SOC2, HIPAA, audit logging)"),
        ("OBS", "Observability, telemetry setup (OpenTelemetry, Datadog)"),
        ("LOGS", "Log handling, structured logging, ELK stack"),
        ("METRICS", "Application metrics, Prometheus counters, Grafana dashboards"),
    ],
    "Architecture & Maintenance": [
        ("ARCH", "System architecture, ADRs (Architecture Decision Records)"),
        ("SPIKE", "Timeboxed research, technical exploration"),
        ("POC", "Proof of Concept implementations"),
        ("REFACTOR", "Code cleanup, restructuring without changing functionality"),
        ("DEBT", "Explicit technical debt resolution"),
        ("BUG", "Bug fixes, unexpected defect resolutions"),
        ("HOTFIX", "Urgent production patches"),
        ("PERF", "Performance optimizations, memory leak fixes"),
        ("DEPS", "Dependency updates, package upgrades"),
    ],
    "Tooling, DX & Documentation": [
        ("DOCS", "Code documentation, READMEs, developer guides"),
        ("API-DOCS", "OpenAPI, Swagger, Postman collection updates"),
        ("DX", "Developer experience enhancements, local dev tools"),
        ("LINT", "Formatting, linters, static analysis rules"),
        ("BUILD", "Bundlers, compilers, build tool configurations (Vite, Webpack)"),
        ("RELEASE", "Release notes, changelog updates, version tagging"),
    ],
    "AI & Data Science (Modern Stack)": [
        ("AI", "Artificial intelligence, model integrations"),
        ("LLM", "Prompt engineering, context window handling, vector stores"),
        ("RAG", "Retrieval-Augmented Generation context systems"),
        ("DATA", "Training dataset preparation, data cleaning"),
    ],
}

CATEGORIES: List[str] = list(VOCABULARY)

TAGS: Dict[str, str] = {
    tag: purpose for entries in VOCABULARY.values() for tag, purpose in entries
}

# Tags are matched case-insensitively, so every lookup goes through this upper-cased
# index and comes back out carrying the spelling this file authored.
CANONICAL: Dict[str, str] = {tag.upper(): tag for tag in TAGS}

_CATEGORY_BY_TAG: Dict[str, str] = {
    tag.upper(): category for category, entries in VOCABULARY.items() for tag, _ in entries
}

UI_TAG: str = "UI"

# A leading tag is only a tag when it is bracketed at position zero and introduces the
# title; anything later is prose that merely mentions the tag.
_LEADING_TAG_RE = re.compile(r"^\[([^\[\]]+)\](?:[ \t]+(.*))?$")


def is_tag(name: str) -> bool:
    """Whether ``name`` is a tag in the vocabulary. Case-insensitive."""
    return name.upper() in CANONICAL


def category_of(tag: str) -> Optional[str]:
    """The category a tag belongs to, or None."""
    return _CATEGORY_BY_TAG.get(tag.upper())


def tag_prefix(tag: str) -> str:
    """The markdown prefix a compiler writes for a tag, e.g. "[UI] " with a trailing space."""
    return f"[{CANONICAL.get(tag.upper(), tag)}] "


def split_tag(title: str) -> Tuple[str, Optional[str]]:
    """Splits a leading bracketed tag off a title.

    Returns (clean title, canonical tag) or (title unchanged, None) when there is no
    leading tag *in the vocabulary*. Only a tag at the very start counts: prose that
    merely mentions one, or a bracketed token that is not in the vocabulary such as
    "[WIP]", must be left in the title untouched.

    Case-insensitive, and the returned tag is the canonical spelling.
    """
    match = _LEADING_TAG_RE.match(title)
    if not match:
        return title, None
    canonical = CANONICAL.get(match.group(1).strip().upper())
    if canonical is None:
        return title, None
    return (match.group(2) or "").strip(), canonical
