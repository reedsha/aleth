// ui/js/visuals.js — Toast notifications and the offline workflow simulator.
function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  DOM.toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(20px)";
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

function simulateWorkflow(prompt) {
  setTimeout(() => {
    handleAgentEvent({ type: "architect_spawn" });
    handleAgentEvent({ type: "log", agent: "software-architect", text: `> Processing directive: "${prompt}"\n> Anchoring state via ${state.activePlan} (plan.json)...\n` });
  }, 400);

  setTimeout(() => {
    handleAgentEvent({ type: "log", agent: "software-architect", text: "> Evaluated complexity: Core Architecture.\n> Gatekeeper Routing: Spawning 'Senior Backend Coder' (coder-deep).\n", log_type: "decision" });
    handleAgentEvent({ type: "delegation", target_agent: "coder-deep", target_name: "Senior Backend Coder" });
  }, 1400);

  setTimeout(() => {
    handleAgentEvent({ type: "coder_spawn", agent: "coder-deep", name: "Senior Backend Coder", model: "Full Shell Access" });
    handleAgentEvent({ type: "log", agent: "coder-deep", text: `> Sub-agent active.\n> Inspecting ${state.activePlan} requirements...\n` });
  }, 2200);

  setTimeout(() => {
    handleAgentEvent({ type: "tool_call", agent: "coder-deep", tool: "execute_shell_command", args: { command: "python --version" }, description: "Checking runtime" });
    handleAgentEvent({ type: "tool_result", agent: "coder-deep", tool: "execute_shell_command", result: "Python 3.12.3" });
    handleAgentEvent({ type: "tool_call", agent: "coder-deep", tool: "write_file", args: { filename: "weather_api.py" }, description: "Writing code" });
    handleAgentEvent({ type: "tool_result", agent: "coder-deep", tool: "write_file", result: "Created weather_api.py" });
    handleAgentEvent({ type: "coder_summary", agent: "coder-deep", summary: { title: "Sub-Agent Implementation Complete", deliverables: ["Created weather_api.py", "Added automated tests", `Logged milestone to plan.json and ${state.activePlan}`] } });
  }, 3200);

  setTimeout(() => {
    handleAgentEvent({ type: "architect_summary", agent: "software-architect", summary: { title: "Lead Architect Verification & Handoff", deliverables: [`Audited standardized updates in ${state.activePlan}`, "Syntax and interfaces validated"], proposals: ["Integrate live external API provider", "Add persistent database storage", "Containerize service with Docker"] } });
    handleAgentEvent({ type: "workflow_complete", status: "completed" });
  }, 4200);
}
