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
machine. It defaults to whatever ``ALETH_MODEL`` names, else ``policy/free``.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from tools.payloads import LayaClassifierPayload, validated

# (directive, expected intent, expected tag) -- hand-labelled, so accuracy means something.
# Tags come from the project's own vocabulary (``tools/task_tags``): the UI rows are the
# ones the four-word heuristic vocabulary drops, and the last row is a task with no
# descriptive tag of its own.
LABELLED: List[Tuple[str, str, str]] = [
    ("make the sidebar collapse animation faster", "code", "FE"),
    ("fix the modal styling", "code", "FE"),
    ("add a button to the toolbar", "code", "FE"),
    ("the preview pane shows a white screen", "code", "FE"),
    ("tweak the header padding", "code", "FE"),
    ("add a login endpoint", "code", "API"),
    ("write tests for the weather API", "code", "TEST"),
    ("add a users table with a migration", "code", "DB"),
    ("refactor the whole orchestration layer", "code", "BE"),
    ("what does the plan parser actually do?", "admin", "BE"),
]


def classifier_prompt() -> str:
    """The llm baseline's prompt, offering the same vocabulary the checkpoint is offered.

    Built from ``tools.task_tags`` so the two columns are answering the same question --
    otherwise the comparison measures the prompt, not the engine.
    """
    from tools.task_tags import NOTHING, ORDER

    keys = ", ".join(f'"{tag.lower()}"' for tag in ORDER) + f', "{NOTHING}"'
    return (
        "You are a fast intent router for a coding agent. Output ONLY compact JSON:\n"
        '{"intent":"admin"|"code","tag":' + keys + ",\"confidence\":0.0-1.0}\n"
        "admin = a question or analysis answered WITHOUT writing code. "
        "code = needs files written or changed. "
        f'Use "{NOTHING}" when no tag fits.'
    )


def plan_corpus() -> List[str]:
    """Every task and sub-step title in the active plan, in document order.

    Real roadmap text, not invented examples: if Laya cannot tag this project's own
    backlog, the number is uninteresting.
    """
    from tools.plan_parser import parse_markdown_to_plan_dict
    from tools.workspace import get_plan_markdown_path

    with open(get_plan_markdown_path(), "r", encoding="utf-8") as handle:
        parsed = parse_markdown_to_plan_dict(handle.read())
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
                {"role": "system", "content": classifier_prompt()},
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
            validated(LayaClassifierPayload, payload)
        except (ValueError, TypeError, ValidationError):
            return None
        return (
            str(payload.get("intent", "")).lower(),
            str(payload.get("tag", "")).lower(),
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
        return None if verdict is None else (verdict.intent, (verdict.tag or "").lower())

    engines.append(("checkpoint", via_checkpoint))
    if llm is not None:
        engines.append(("llm", llm.classify))

    print()
    print("ACCURACY -- hand-labelled set")
    print(_row("directive") + "".join(name.ljust(26) for name, _ in engines))
    hits = {name: 0 for name, _ in engines}
    for text, want_intent, want_tag in LABELLED:
        cells = []
        for name, fn in engines:
            got = fn(text)
            if got is None:
                cells.append("off-contract".ljust(26))
                continue
            intent, tag = got
            mark = "OK" if tag == want_tag.lower() else "MISS"
            hits[name] += tag == want_tag.lower()
            cells.append(f"{intent}/{tag or 'none'} {mark}".ljust(26))
        print(_row(text) + "".join(cells))
    print()
    total = len(LABELLED)
    for name, _ in engines:
        print(f"  {name:11} tag accuracy: {hits[name]}/{total}")
    if llm is not None:
        print(
            f"  (llm reference: {llm.tokens} tokens over {llm.calls} calls)"
        )


def corpus_report(llm: Optional[LlmEngine], sample: int) -> None:
    from agents import laya as heuristic

    titles = plan_corpus()
    checkpoint = _checkpoint_engine(verbose=False)

    disagreements: List[Tuple[str, str, str]] = []
    distribution: Dict[str, int] = {}
    heuristic_ui = checkpoint_ui = 0
    checkpoint_ms = 0.0
    for title in titles:
        h = heuristic.classify(title)
        started = time.time()
        verdict = checkpoint.classify(title)
        checkpoint_ms += (time.time() - started) * 1000
        if verdict is None:
            continue
        h_tag = (h.tag or "").lower()
        c_tag = (verdict.tag or "").lower()
        heuristic_ui += h_tag == "fe"
        checkpoint_ui += c_tag == "fe"
        distribution[c_tag or "none"] = distribution.get(c_tag or "none", 0) + 1
        if h_tag != c_tag:
            disagreements.append((title, h_tag, c_tag))

    print()
    print(f"CORPUS -- this plan's own {len(titles)} task and sub-step titles")
    print(
        f"  tagged [FE] by the heuristic: {heuristic_ui}    "
        f"by the checkpoint: {checkpoint_ui}"
    )
    print("  tags the checkpoint chose: " + ", ".join(
        f"{tag}={count}" for tag, count in sorted(distribution.items(), key=lambda kv: -kv[1])
    ))
    print(f"  tag disagreements: {len(disagreements)}")
    for title, h_tag, c_tag in disagreements:
        print(f"    {_row(title)} heuristic={h_tag or 'none':8} checkpoint={c_tag or 'none'}")
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
    return verdict.intent, (verdict.tag or "").lower()


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
    parser.add_argument("--model", default=os.environ.get("ALETH_MODEL", "policy/free"))
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
