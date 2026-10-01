// ui/js/visuals.js — The offline workflow simulator.
//
// The one visual concern left here is the simulator that drives a demo run in a plain
// browser (there is no desktop bridge). It feeds the same inbound event funnel the real
// backend does, so the demo exercises the real render path rather than a parallel one.
// `showToast` used to live here too; it moved to ui/js/notify.js so that showing a toast no
// longer means importing this module -- which imports the event handler -- and closing the
// cycle that made the import graph strongly connected.
import { handleAgentEvent } from "./agent-events.js";
import { state } from "./store.js";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// One ordered sequence, not five independent timers. The steps still fire at the same
// absolute offsets as before, but through a single awaited chain, so a slow step delays the
// next instead of racing it. Each step's payloads read `state.activePlan` when they run,
// exactly as the closures did before. The `400`/`1400`/... delays are *sequencing*, not
// animation, so they are the ones in scope here; the toast fade in notify.js stays.
const SIMULATION_STEPS = [
  {
    at: 400,
    run: (prompt) => {
      handleAgentEvent({ type: "architect_spawn" });
      handleAgentEvent({ type: "log", agent: "software-architect", text: `> Processing directive: "${prompt}"\n> Anchoring state via ${state.activePlan} (plan.json)...\n` });
    }
  },
  {
    at: 1400,
    run: () => {
      handleAgentEvent({ type: "log", agent: "software-architect", text: "> Evaluated complexity: Core Architecture.\n> Gatekeeper Routing: Spawning 'Senior Backend Coder' (coder-deep).\n", log_type: "decision" });
      handleAgentEvent({ type: "delegation", target_agent: "coder-deep", target_name: "Senior Backend Coder" });
    }
  },
  {
    at: 2200,
    run: () => {
      handleAgentEvent({ type: "coder_spawn", agent: "coder-deep", name: "Senior Backend Coder", model: "Full Shell Access" });
      handleAgentEvent({ type: "log", agent: "coder-deep", text: `> Sub-agent active.\n> Inspecting ${state.activePlan} requirements...\n` });
    }
  },
  {
    at: 3200,
    run: () => {
      handleAgentEvent({ type: "tool_call", agent: "coder-deep", tool: "execute_shell_command", args: { command: "python --version" }, description: "Checking runtime" });
      handleAgentEvent({ type: "tool_result", agent: "coder-deep", tool: "execute_shell_command", result: "Python 3.12.3" });
      handleAgentEvent({ type: "tool_call", agent: "coder-deep", tool: "write_file", args: { filename: "weather_api.py" }, description: "Writing code" });
      handleAgentEvent({ type: "tool_result", agent: "coder-deep", tool: "write_file", result: "Created weather_api.py" });
      handleAgentEvent({ type: "coder_summary", agent: "coder-deep", summary: { title: "Sub-Agent Implementation Complete", deliverables: ["Created weather_api.py", "Added automated tests", `Logged milestone to plan.json and ${state.activePlan}`] } });
    }
  },
  {
    at: 4200,
    run: () => {
      handleAgentEvent({ type: "architect_summary", agent: "software-architect", summary: { title: "Lead Architect Verification & Handoff", deliverables: [`Audited standardized updates in ${state.activePlan}`, "Syntax and interfaces validated"], proposals: ["Integrate live external API provider", "Add persistent database storage", "Containerize service with Docker"] } });
      handleAgentEvent({ type: "workflow_complete", status: "completed" });
    }
  }
];

export async function simulateWorkflow(prompt) {
  let previous = 0;
  for (const step of SIMULATION_STEPS) {
    const wait = step.at - previous;
    previous = step.at;
    if (wait > 0) await sleep(wait);
    step.run(prompt);
  }
}
