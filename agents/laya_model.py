"""The Laya checkpoint behind System 1's decision seam. Off by default.

``agents/laya.py`` answers a directive with a hand-written word list: no model, no
tokens, and a rule you can read. The Laya Router answers the *same* two questions --
is this a request to change code or a request for analysis, and what part of the
system does it concern -- with a calibrated probability instead, on a 421M-parameter
checkpoint. This module is the adapter between the two, so which engine is live is a
deployment decision (one environment variable) rather than an edit to a call site.

``LAYA_BACKEND`` names the backend:

    unset, "heuristic"   the word list. The default, and the permanent fallback.
    "model"              the checkpoint, via the ``laya`` package.

The checkpoint is roughly 0.8 GB of weights that the package downloads from Hugging
Face on the first ``predict`` call, which is why "model" is opt-in and never a
default. Any other value is reported and treated as the heuristic, so a typo in a
deployment file costs a warning rather than a broken decision path.

Two things are deliberately absent:

* **Non-determinism.** A decision function that answers differently before and after
  a background load is worse than a slow one, so the first call waits for the
  checkpoint rather than being silently answered by the other engine.
* **A startup hook.** Warming the model before the first decision needs the launcher
  to call :func:`warm_up`, which is its own change. Until then the load is paid once,
  on the first gatekeeper decision, and cached for the process lifetime.

The dependency runs one way only: this module imports the engine, and the engine
never imports this one, so ``agents/laya.py`` stays free of a model runtime.
"""

import os
import threading
from typing import Any, Callable, Dict, Mapping, Optional

from agents.laya import (
    DOMAIN_API,
    DOMAIN_CORE,
    DOMAIN_DB,
    DOMAIN_DOCS,
    DOMAIN_GENERAL,
    DOMAIN_TESTS,
    DOMAIN_UI,
    INTENT_ADMIN,
    INTENT_CODE,
    Verdict,
    classify as heuristic_classify,
    coder_for_domain,
)

__all__ = [
    "BACKEND_HEURISTIC",
    "BACKEND_MODEL",
    "DEFAULT_MODEL",
    "ENV_BACKEND",
    "Backend",
    "Resolver",
    "classify",
    "questions",
    "reset",
    "warm_up",
]


ENV_BACKEND = "LAYA_BACKEND"
BACKEND_HEURISTIC = "heuristic"
BACKEND_MODEL = "model"

# The checkpoint these questions are written against. The package also ships
# "multilingual" and "typed-decisions"; neither is a drop-in here, because the Router
# selects a checkpoint by matching a question set against that checkpoint's own
# workflow. Pinning the model keeps the choice visible instead of leaving it to the
# router's own heuristics.
DEFAULT_MODEL = "convaiinnovations/laya"

# The questions below address the payload by name (`request`), so the state is passed
# as a mapping under this key rather than as a bare string.
_STATE_KEY = "request"

# The criteria keys the checkpoint answers with, mapped onto the engine's vocabulary.
# These two tables are the contract between the engines: a label that is not here is
# off-contract, and the word list answers instead of guessing.
_INTENT_BY_LABEL = {"analysis": INTENT_ADMIN, "implementation": INTENT_CODE}
_DOMAIN_BY_LABEL = {
    "ui": DOMAIN_UI,
    "api": DOMAIN_API,
    "db": DOMAIN_DB,
    "tests": DOMAIN_TESTS,
    "docs": DOMAIN_DOCS,
    "core": DOMAIN_CORE,
    "general": DOMAIN_GENERAL,
}


def _report(message: str) -> None:
    print(f"[Laya] {message}")


def questions() -> Dict[str, Any]:
    """The two decisions ``classify`` makes, as the checkpoint's native question set.

    The criteria keys *are* the answer contract: they are what
    :meth:`Backend.classify` maps back onto the engine's vocabulary, so the two
    engines cannot drift apart about what "analysis" or "ui" means. The criteria text
    is what the checkpoint reads, so rewording it changes accuracy even though the
    labels stay put.
    """
    return {
        "intent": {
            "type": "choice",
            "instructions": (
                "In `request`, is the user asking for existing code to be explained, "
                "reviewed or assessed, or asking for code to be written or changed?"
            ),
            "criteria": {
                "analysis": "explain, review, assess or report on what already exists",
                "implementation": (
                    "write, change, fix, remove or run code or configuration"
                ),
            },
        },
        "domain": {
            "type": "choice",
            "instructions": "What part of the system does `request` concern?",
            "criteria": {
                "ui": "user interface, layout, styling, views or front-end behaviour",
                "api": "HTTP endpoints, routes, handlers, servers or integrations",
                "db": "databases, schemas, migrations, queries or persistence",
                "tests": "tests, coverage, fixtures, mocks or assertions",
                "docs": "documentation, readmes, changelogs or comments",
                "core": "core algorithms, parsers, engines, authentication or concurrency",
                "general": "none of the other options fits",
            },
        },
    }


def _answer(answers: Mapping[str, Any], question: str) -> Optional[Mapping[str, Any]]:
    answer = answers.get(question)
    return answer if isinstance(answer, Mapping) else None


def _choice(answers: Mapping[str, Any], question: str) -> Optional[str]:
    """The criteria key the checkpoint chose, or ``None`` if it answered off-contract.

    The package reports a ``choice`` answer as ``{"type": "choice", "choice": key}``.
    A missing question, another question type, or a key outside the criteria all mean
    the answer cannot be mapped, and the caller falls back rather than guessing.
    """
    answer = _answer(answers, question)
    if answer is None or answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    return choice if isinstance(choice, str) else None


def _confidence(answers: Mapping[str, Any], question: str) -> float:
    """The checkpoint's calibrated answer confidence, else a neutral 0.5.

    The package reports an ``answer_confidence`` that is comparable across question
    types, which is why it is preferred over ``confidence`` (max-probability for a
    no/yes question, normalized entropy for a choice).
    """
    answer = _answer(answers, question)
    if answer is not None:
        for key in ("answer_confidence", "confidence"):
            value = answer.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, min(1.0, float(value)))
    return 0.5


class Backend:
    """Present a ``laya.Router`` as a decision source that returns ``Verdict``s.

    The router is injected rather than constructed here so the mapping can be tested
    without the checkpoint, and so a different implementation (a stub, a service, a
    future version of the package) needs no change to this file.
    """

    def __init__(
        self,
        router: Any,
        model: str = DEFAULT_MODEL,
        state_key: str = _STATE_KEY,
    ):
        self._router = router
        self.model = model
        self._state_key = state_key

    def classify(
        self, text: str, context: Optional[Mapping[str, Any]] = None
    ) -> Optional[Verdict]:
        """One decision from the checkpoint, or ``None`` if it answered off-contract.

        Returning ``None`` rather than raising keeps "answered something outside the
        criteria" and "failed outright" on the same path at the call site, which is
        where the word list takes over.
        """
        result = self._router.predict(
            {self._state_key: text or ""}, questions(), model=self.model
        )
        answers = (result or {}).get("answers") or {}

        intent_label = _choice(answers, "intent")
        domain_label = _choice(answers, "domain")
        intent = _INTENT_BY_LABEL.get(intent_label or "")
        domain = _DOMAIN_BY_LABEL.get(domain_label or "")
        if intent is None or domain is None:
            _report(
                "checkpoint answered off-contract "
                f"(intent={intent_label!r}, domain={domain_label!r}); using the word list"
            )
            return None

        confidence = _confidence(answers, "intent")
        return Verdict(
            intent=intent,
            domain=domain,
            coder=coder_for_domain(domain),
            confidence=confidence,
            reasons=(
                f"laya checkpoint answered intent={intent_label!r}, domain={domain_label!r}",
                f"calibrated answer confidence {confidence:.2f}",
            ),
        )


def _load_router() -> Any:
    """Constructs the package's Router.

    Imported inside the function so that this module -- and therefore anything that
    imports it, including the decision path on a machine without the checkpoint --
    costs nothing until the model backend is actually selected.
    """
    try:
        from laya import Router
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"the 'laya' package is not installed ({exc}); "
            f"install it or unset {ENV_BACKEND}"
        ) from exc
    except ImportError as exc:
        raise RuntimeError(
            f"the installed 'laya' package has no Router ({exc}); 'agents' may be "
            f"shadowing it on sys.path"
        ) from exc
    return Router()


class Resolver:
    """Decides once, for the process lifetime, which engine answers.

    Holding the decision here rather than re-reading the environment per call is what
    keeps a decision cheap: the gate is read at most once, and the checkpoint is
    constructed at most once, however many directives arrive.
    """

    def __init__(self, enabled: bool, load: Callable[[], Any]):
        self._enabled = enabled
        self._load = load
        self._lock = threading.Lock()
        self._backend: Optional[Backend] = None
        self._failure: Optional[BaseException] = None
        self._ready = False

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        load: Optional[Callable[[], Any]] = None,
    ) -> "Resolver":
        """Reads ``LAYA_BACKEND``. An unrecognised name is reported and read as the heuristic."""
        source = os.environ if environ is None else environ
        raw = (source.get(ENV_BACKEND) or "").strip().lower()
        if raw and raw not in (BACKEND_HEURISTIC, BACKEND_MODEL):
            _report(
                f"unknown {ENV_BACKEND}={raw!r}; using the {BACKEND_HEURISTIC} engine"
            )
            raw = BACKEND_HEURISTIC
        return cls(raw == BACKEND_MODEL, load or _load_router)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def failure(self) -> Optional[BaseException]:
        """Why the checkpoint is unavailable, once a load has been attempted and failed."""
        return self._failure

    def backend(self) -> Optional[Backend]:
        """The checkpoint, loading it on first use. ``None`` disables the model path.

        The lock is held across the load on purpose: a second thread arriving while the
        weights are being read waits for the same backend instead of starting a second
        copy of it.
        """
        if not self._enabled:
            return None
        with self._lock:
            if not self._ready:
                self._ready = True
                try:
                    self._backend = Backend(self._load())
                except BaseException as exc:  # a load failure must not break a decision
                    self._failure = exc
                    _report(f"model backend unavailable, using the word list: {exc}")
            return self._backend

    def classify(
        self, text: str, context: Optional[Mapping[str, Any]] = None
    ) -> Verdict:
        """The seam: the checkpoint when it is live, the word list otherwise."""
        # An explicit [UI] tag is a fact about the task, not a judgement, so it never
        # reaches the checkpoint: the engine already knows the answer.
        if (context or {}).get("is_ui"):
            return heuristic_classify(text, context)

        backend = self.backend()
        if backend is None:
            return heuristic_classify(text, context)

        try:
            verdict = backend.classify(text, context)
        except BaseException as exc:  # inference must never take the decision path down
            _report(f"model decision failed, using the word list: {exc}")
            return heuristic_classify(text, context)
        return verdict if verdict is not None else heuristic_classify(text, context)


_RESOLVER: Optional[Resolver] = None
_RESOLVER_LOCK = threading.Lock()


def _resolver() -> Resolver:
    global _RESOLVER
    with _RESOLVER_LOCK:
        if _RESOLVER is None:
            _RESOLVER = Resolver.from_env()
        return _RESOLVER


def classify(
    text: str, context: Optional[Mapping[str, Any]] = None
) -> Verdict:
    """Decides what a directive is asking for, and who should handle it.

    Signature-compatible with :func:`agents.laya.classify`, so the two engines are
    interchangeable at the call site. This is the one the Gatekeeper uses.
    """
    return _resolver().classify(text, context)


def warm_up() -> None:
    """Loads the checkpoint ahead of the first decision. A no-op unless the gate is on.

    Wire this from the launcher to move the one-off load off the first click: until
    something calls it, the first gatekeeper decision pays for the load.
    """
    _resolver().backend()


def reset() -> None:
    """Forgets the resolved backend, so the gate is re-read on the next decision."""
    global _RESOLVER
    with _RESOLVER_LOCK:
        _RESOLVER = None
