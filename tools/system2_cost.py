"""What a System 2 turn costs, measured against a real route.

Laya's value is the Coder spawn it prevents, and a Coder spawn is not free: the agent
loop re-sends the whole conversation every turn, and the turn-1 payload already carries
the system prompt, the tool schemas and the task. Sizing that is what turns "Laya saves
tokens" into a number.

Everything sent here is assembled from this repository's own artifacts -- the real
``agents/`` system prompts, the real tool schemas behind ``all_file_tools`` /
``execute_restricted_command``, and the actual ``PLAN.md`` / ``plan.json`` the Coder is instructed
to read first -- and the token counts are read back from the API's own ``usage``, not
estimated from character counts.

Two things are deliberately **not** modelled, and both make the real numbers *larger*
than what is reported here:

* the framework's own overhead -- ``create_deep_agent`` builds the architect with its
  subagents, and the harness's tool wiring and instructions are part of every real call;
* the Code the model actually emits, beyond a fixed output budget per turn.

The turn count is the one genuinely modelled input, so it is a parameter rather than a
hidden constant.

Run it with::

    ./venv/Scripts/python.exe -m tools.system2_cost
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from tools.payloads import CostReportPayload, validated

DEFAULT_MODEL = "policy/free"

# A turn's own output: a tool call or a short patch summary. Real Coders write more; this
# is the floor, not the estimate.
OUTPUT_TOKENS_PER_TURN = 400


def _openai_tools(bundle: List[Any]) -> List[Dict[str, Any]]:
    """The bundle as the API sees it, so its schema tokens are really billed."""
    tools = []
    for tool in bundle:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": getattr(tool, "args", None) or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _read_workspace_file(name: str) -> str:
    from tools.workspace import get_project_dir

    path = os.path.join(get_project_dir(), name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        print(f"  (could not read {name}: {exc})")
        return ""


def _payloads() -> Dict[str, Any]:
    """The two roles' real system prompts and tool schemas.

    The shell schemas come from the MCP exec server's own ``tools/list`` -- the same source
    the agents bind from -- rather than from a static catalog, which no longer exists. The
    session is spawned for the measurement and reaped when it ends.
    """
    from agents import architect, coders
    from orchestration.agent_catalog import resolve_prompt_variables
    from orchestration.mcp_session import MCPSessionContext
    from tools.workspace import get_active_plan_filename, get_project_dir

    plan_name = get_active_plan_filename()
    with MCPSessionContext(get_project_dir()) as session:
        def bound(role: str):
            return list(session.get_bound_tools(role))

        admin_tools = bound("architect")
        coder_tools = bound("coder")

        return {
            "admin": {
                "system": resolve_prompt_variables(architect.ARCHITECT_SYSTEM_PROMPT, plan_name),
                "tools": _openai_tools(admin_tools),
            },
            "coder_deep": {
                "system": resolve_prompt_variables(coders.coder_deep.system_prompt, plan_name),
                "tools": _openai_tools(coder_tools),
            },
            "coder_standard": {
                "system": resolve_prompt_variables(coders.coder_standard.system_prompt, plan_name),
                "tools": _openai_tools(coder_tools),
            },
        }


class Meter:
    """Real calls, real ``usage``. The route rejects temperature 0, so it is not sent."""

    def __init__(self, model: str):
        from openai import OpenAI

        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        if not base or not key:
            raise RuntimeError("OPENAI_BASE_URL / OPENAI_API_KEY are not set; check .env")
        self.model = model
        self._client = OpenAI(api_key=key, base_url=base.rstrip("/"))

    def measure(self, system: str, tools: List[Dict[str, Any]], messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """One real turn. Returns its billed input/output token counts."""
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=64,
            messages=[{"role": "system", "content": system}] + messages,
            tools=tools or None,
        )
        usage = response.usage
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }


def main(argv: List[str] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=os.environ.get("DEEPAGENTS_MODEL", DEFAULT_MODEL))
    parser.add_argument("--turns", type=int, default=4, help="Coder loop turns to total over")
    parser.add_argument("--reads", type=int, default=2, help="files read per Coder turn")
    args = parser.parse_args(argv)

    from env_boot import load_environment

    load_environment()

    payloads = _payloads()
    plan_md = _read_workspace_file("PLAN.md")
    plan_json = _read_workspace_file("plan.json")
    read_blob = f"Contents of PLAN.md:\n{plan_md}\n\nContents of plan.json:\n{plan_json}"

    meter = Meter(args.model)
    print(f"route: {args.model} via {os.environ.get('OPENAI_BASE_URL')}")
    print()

    # 1. The admin answer -- what the Gatekeeper does instead of delegating.
    admin = payloads["admin"]
    directive = "Review the plan and report which tasks look like UI work."
    admin_usage = meter.measure(
        admin["system"], admin["tools"], [{"role": "user", "content": directive}]
    )
    print("ADMIN ANSWER (no spawn)")
    print(f"  system prompt + 1 tool schema + directive  ->  {admin_usage['prompt_tokens']:,} input tokens")
    print()

    # 2. The Coder spawn, turn 1, and the same turn once the mandated reads land.
    coder = payloads["coder_deep"]
    task = "Build the expander panel so it visually merges with the active action button"
    turn1 = meter.measure(coder["system"], coder["tools"], [{"role": "user", "content": task}])
    turn2 = meter.measure(
        coder["system"],
        coder["tools"],
        [
            {"role": "user", "content": task},
            {"role": "user", "content": read_blob},
        ],
    )
    read_cost = turn2["prompt_tokens"] - turn1["prompt_tokens"]
    print("CODER SPAWN")
    print(f"  system prompt + 4 tool schemas + task      ->  {turn1['prompt_tokens']:,} input tokens")
    print(f"  after reading PLAN.md + plan.json          ->  {turn2['prompt_tokens']:,} input tokens")
    print(f"  the two mandated reads cost                ->  {read_cost:,} tokens")
    print()

    # 3. The loop. Turn k re-sends everything, so the bill grows quadratically.
    first = turn1["prompt_tokens"]
    per_turn_growth = read_cost + OUTPUT_TOKENS_PER_TURN
    total_in = sum(first + i * per_turn_growth for i in range(args.turns))
    total_out = args.turns * OUTPUT_TOKENS_PER_TURN
    print(f"THE LOOP (modelled: {args.turns} turns, {args.reads} reads each)")
    print(f"  turn 1 input:                              {first:,} tokens")
    print(f"  each further turn adds:                    {per_turn_growth:,} tokens")
    print(f"  total input across {args.turns} turns:                  {total_in:,} tokens")
    print(f"  total output across {args.turns} turns:                 {total_out:,} tokens")
    print(f"  ONE SPAWN:                                 {total_in + total_out:,} tokens")
    print()
    print("WHAT LAYA GATES")
    print(f"  a spawn costs about {total_in + total_out:,} tokens")
    print(f"  an admin answer costs about {admin_usage['prompt_tokens'] + admin_usage['completion_tokens']:,}")
    print(f"  so each spawn Laya prevents saves roughly {total_in + total_out:,} tokens")
    print()
    print("Not modelled, and both would raise these numbers: the framework's own")
    print("instructions and subagent wiring, and the code a Coder actually emits.")

    if os.environ.get("DEEPAGENTS_COST_JSON"):
        print(json.dumps(validated(CostReportPayload, {
            "admin_input": admin_usage["prompt_tokens"],
            "spawn_turn1_input": first,
            "reads_cost": read_cost,
            "spawn_total": total_in + total_out,
            "turns": args.turns,
        })))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
