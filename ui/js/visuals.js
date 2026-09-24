// ui/js/visuals.js — Toast notifications, orb animation, and offline workflow simulator.
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

function initOrbAnimation() {
  const canvas = document.getElementById("orbCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  // Decoration only: a backend that refuses to hand out a 2d context must never
  // propagate a throw into the bootstrap that wires up the rest of the UI.
  if (!ctx) {
    console.warn("[DeepAgents UI] orb animation unavailable: no 2d canvas context");
    return;
  }
  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const radius = 88;

  let angle = 0;

  function render() {
    angle += 0.012;
    ctx.clearRect(0, 0, w, h);

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.clip();

    ctx.fillStyle = "#030408";
    ctx.fill();

    const grad1 = ctx.createLinearGradient(
      cx + Math.cos(angle) * 70,
      cy + Math.sin(angle) * 70,
      cx - Math.cos(angle) * 70,
      cy - Math.sin(angle) * 70
    );
    grad1.addColorStop(0, "rgba(56, 189, 248, 0.95)");
    grad1.addColorStop(0.25, "rgba(147, 51, 234, 0.9)");
    grad1.addColorStop(0.5, "rgba(236, 72, 153, 0.95)");
    grad1.addColorStop(0.75, "rgba(249, 115, 22, 0.85)");
    grad1.addColorStop(1, "rgba(37, 99, 235, 0.9)");

    ctx.fillStyle = grad1;
    ctx.beginPath();
    ctx.ellipse(
      cx + Math.sin(angle * 1.5) * 14,
      cy + Math.cos(angle * 1.2) * 14,
      radius * 0.95,
      radius * 0.75,
      angle * 0.8,
      0,
      Math.PI * 2
    );
    ctx.fill();

    const grad2 = ctx.createRadialGradient(
      cx + Math.cos(angle * 0.7) * 35,
      cy + Math.sin(angle * 0.7) * 35,
      8,
      cx,
      cy,
      radius
    );
    grad2.addColorStop(0, "rgba(255, 255, 255, 0.85)");
    grad2.addColorStop(0.2, "rgba(96, 165, 250, 0.8)");
    grad2.addColorStop(0.55, "rgba(10, 15, 30, 0.92)");
    grad2.addColorStop(0.85, "rgba(192, 132, 252, 0.75)");
    grad2.addColorStop(1, "rgba(6, 182, 212, 0.4)");

    ctx.fillStyle = grad2;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.92, 0, Math.PI * 2);
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(cx - 50, cy + 30);
    ctx.bezierCurveTo(
      cx - 20 + Math.sin(angle * 2) * 20,
      cy - 60 + Math.cos(angle) * 20,
      cx + 40 + Math.cos(angle * 1.8) * 20,
      cy - 10 + Math.sin(angle) * 15,
      cx + 60,
      cy + 40
    );
    ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
    ctx.lineWidth = 14;
    ctx.lineCap = "round";
    ctx.filter = "blur(6px)";
    ctx.stroke();
    ctx.filter = "none";

    ctx.restore();

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.18)";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();

    requestAnimationFrame(render);
  }

  render();
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
