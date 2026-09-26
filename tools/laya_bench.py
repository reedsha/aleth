"""What Laya's decision is worth, measured rather than asserted.

Laya exists to make a decision without spending a token. This script puts a number on
that, using this repository's own ``PLAN.md`` as the corpus, and three engines behind the
same seam:

    heuristic    ``agents/laya.py``            local, free, instant
    checkpoint   ``agents/laya_model.py``      local, free, ~0.8s (CPU)
    llm          an OpenAI-compatible route     measured tokens, network round-trip

Two questions get answered:

1. **Accuracy.** On a hand-labelled set, which engine gets the domain right?
2. **Cost.** What does the roadmap's own task list cost to classify, per engine?

The token figure is the point of the exercise: the LLM column is a *measured* bill, not an
estimate. Everything Laya answers is a call the LLM column does not have to pay for.

Run it with::

    ./venv/Scripts/python.exe -m tools.laya_bench

The LLM column is opt-in (``--llm``) because it is the only column that leaves the
machine. It defaults to whatever ``DEEPAGENTS_MODEL`` names, else ``policy/free``.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# (directive, expected intent, expected domain) -- hand-labelled, so accuracy means
# something. The UI rows are the ones the four-word heuristic vocabulary drops.
LABELLED: List[Tuple[str, str, str]] = [
    ("make the sidebar collapse animation faster", "code", "ui"),
    ("fix the modal styling", "code", "ui"),
    ("add a button to the toolbar", "code", "ui"),
    ("the preview pane shows a white screen", "code", "ui"),
    ("tweak the header padding", "code", "ui"),
    ("add a login endpoint", "code", "api"),
    ("write tests for the weather API", "code", "tests"),
    ("add a users table with a migration", "code", "db"),
    ("refactor the whole orchestration layer", "code", "core"),
    ("what does the plan parser actually do?", "admin", "core"),
]

CLASSIFIER_PROMPT = (
    "You are a fast intent router for a coding agent. Output ONLY compact JSON:\n"
    '{"intent":"admin"|"code","domain":"ui"|"api"|"db"|"tests"|"docs"|"core"|"general",'
    '"confidence":0.0-1.0}\n'
    "admin = a question or analysis answered WITHOUT writing code. "
    "code = needs files written or changed."
)


def plan_corpus() -> List[str]:
    """Every task and sub-step title in the active plan, in document order.

    Real roadmap text, not invented examples: if Laya cannot tag this project's own
    backlog, the number is uninteresting.
    """
    from tools.plan_parser import parse_markdown_to_plan_dict

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "my_project_workspace",
        "PLAN.md",
    )
    with open(path, "r", encoding="utf-8") as f:
        parsed = parse_markdown_to_plan_dict(f.read())
    titles: List[str] = []
    for step in parsed.get("steps", []):
        title = (step.get("title") or "").strip()
        if title:
            titles.append(title)
        for sub in step.get("sub_steps") or []:
            sub_title = (sub.get("title") or "").strip()
            if sub_title:
                titles.append(sub_title)
    return titles


class LlmEngine:
    """The measured baseline: a real OpenAI-compatible call, with its real token usage."""

    def __init__(self, model: str):
        from openai import OpenAI

        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        if not base or not key:
            raise RuntimeError(
                "OPENAI_BASE_URL / OPENAI_API_KEY are not set; "
                "check .env (env_boot.load_environment() must run first)"
            )
        self.model = model
        self._client = OpenAI(api_key=key, base_url=base.rstrip("/"))
        self._greedy = True
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def classify(self, text: str) -> Optional[Tuple[str, str]]:
        """One classification, and the token usage it actually cost.

        Greedy decoding is attempted first because a benchmark wants a reproducible
        answer, but not every route behind a policy tolerates it: some refuse
        ``temperature=0`` outright with "top_p must be 1 when using greedy sampling"
        even when ``top_p`` is 1. Rather than silently sampling at temperature 1 and
        understating the baseline, greedy is dropped once per run and the retry is what
        gets recorded.
        """
        from openai import BadRequestError

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 120,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        if self._greedy:
            payload["temperature"] = 0
            payload["top_p"] = 1
        try:
            response = self._client.chat.completions.create(**payload)
        except BadRequestError as exc:
            if not self._greedy or "greedy" not in str(exc).lower():
                raise
            self._greedy = False
            payload.pop("temperature", None)
            payload.pop("top_p", None)
            response = self._client.chat.completions.create(**payload)
        self.calls += 1
        usage = response.usage
        if usage is not None:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0
        try:
            payload = json.loads(response.choices[0].message.content or "")
        except (ValueError, TypeError):
            return None
        return (
            str(payload.get("intent", "")).lower(),
            str(payload.get("domain", "")).lower(),
        )

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _checkpoint_engine(verbose: bool) -> Any:
    from laya import Router

    from agents import laya_model

    if verbose:
        print("loading the Laya checkpoint (cached after the first run)...", flush=True)
    return laya_model.Backend(Router(), model=laya_model.DEFAULT_MODEL)


def _row(text: str, width: int = 52) -> str:
    return text[: width - 1] + "…" if len(text) >= width else text.ljust(width)


def accuracy_report(llm: Optional[LlmEngine]) -> None:
    from agents import laya as heuristic

    checkpoint = _checkpoint_engine(verbose=True)
    engines: List[Tuple[str, Any]] = [("heuristic", lambda t: _verdict_pair(heuristic.classify(t)))]

    def via_checkpoint(text: str) -> Optional[Tuple[str, str]]:
        verdict = checkpoint.classify(text)
        return None if verdict is None else (verdict.intent, verdict.domain.lower())

    engines.append(("checkpoint", via_checkpoint))
    if llm is not None:
        engines.append(("llm", llm.classify))

    print()
    print("ACCURACY -- hand-labelled set")
    print(_row("directive") + "".join(name.ljust(26) for name, _ in engines))
    hits = {name: 0 for name, _ in engines}
    for text, want_intent, want_domain in LABELLED:
        cells = []
        for name, fn in engines:
            got = fn(text)
            if got is None:
                cells.append("off-contract".ljust(26))
                continue
            intent, domain = got
            mark = "OK" if domain == want_domain else "MISS"
            hits[name] += domain == want_domain
            cells.append(f"{intent}/{domain} {mark}".ljust(26))
        print(_row(text) + "".join(cells))
    print()
    total = len(LABELLED)
    for name, _ in engines:
        print(f"  {name:11} domain accuracy: {hits[name]}/{total}")
    if llm is not None:
        print(
            f"  (llm reference: {llm.tokens} tokens over {llm.calls} calls)"
        )


def corpus_report(llm: Optional[LlmEngine], sample: int) -> None:
    from agents import laya as heuristic

    titles = plan_corpus()
    checkpoint = _checkpoint_engine(verbose=False)

    disagreements: List[Tuple[str, str, str]] = []
    heuristic_ui = checkpoint_ui = 0
    checkpoint_ms = 0.0
    for title in titles:
        h = heuristic.classify(title)
        started = time.time()
        verdict = checkpoint.classify(title)
        checkpoint_ms += (time.time() - started) * 1000
        if verdict is None:
            continue
        h_domain = h.domain.lower()
        c_domain = verdict.domain.lower()
        heuristic_ui += h_domain == "ui"
        checkpoint_ui += c_domain == "ui"
        if h_domain != c_domain:
            disagreements.append((title, h_domain, c_domain))

    print()
    print(f"CORPUS -- this plan's own {len(titles)} task and sub-step titles")
    print(
        f"  tagged [UI] by the heuristic: {heuristic_ui}    "
        f"by the checkpoint: {checkpoint_ui}"
    )
    print(f"  domain disagreements: {len(disagreements)}")
    for title, h_domain, c_domain in disagreements:
        print(f"    {_row(title)} heuristic={h_domain:8} checkpoint={c_domain}")
    if titles:
        print(f"  checkpoint cost: {checkpoint_ms / len(titles):.0f} ms/title, 0 tokens")

    if llm is None:
        return
    print()
    print(f"COST -- measuring the llm route on the first {sample} titles")
    for title in titles[:sample]:
        llm.classify(title)
    if llm.calls:
        print(f"  {llm.calls} classifications, {llm.tokens} tokens "
              f"({llm.prompt_tokens} in / {llm.completion_tokens} out)")
        per = llm.tokens / llm.calls
        print(f"  per classification: {per:.0f} tokens")
        print(
            f"  the same {len(titles)} titles via the llm would cost about "
            f"{per * len(titles):,.0f} tokens"
        )
        print(f"  via Laya (either engine): 0 tokens")


def _verdict_pair(verdict: Any) -> Tuple[str, str]:
    return verdict.intent, verdict.domain.lower()


def main(argv: Optional[List[str]] = None) -> int:
    # Plan titles carry emoji ("## 🌍 Global State Summary"), and the Windows console
    # defaults to cp1252: without this the report dies part-way through on a real title.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--llm",
        action="store_true",
        help="also measure the network route (the only column that costs tokens)",
    )
    parser.add_argument("--model", default=os.environ.get("DEEPAGENTS_MODEL", "policy/free"))
    parser.add_argument("--sample", type=int, default=12, help="titles to measure the llm on")
    args = parser.parse_args(argv)

    from env_boot import load_environment

    load_environment()

    llm = None
    if args.llm:
        try:
            llm = LlmEngine(args.model)
            print(f"llm route: {args.model} via {os.environ.get('OPENAI_BASE_URL')}")
        except RuntimeError as exc:
            print(f"llm route unavailable: {exc}", file=sys.stderr)

    accuracy_report(llm)
    corpus_report(llm, args.sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
