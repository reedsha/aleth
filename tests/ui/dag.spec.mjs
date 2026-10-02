// Structural and behavioural tests for the Artifact DAG and its diff surface (Phase 8).
//
// The DAG is the review surface of the two-phase lifecycle: a milestone is coloured
// strictly by state, a PLANNED node carries the Approve control, and selecting a node shows
// the artifact's own AST targets against the bytes they would replace. These tests drive it
// through the real inbound wire (`window.__deepAgentsBus.receive`), so the strict
// parse/validate path and everything downstream of it is exercised, not stubbed.

import { test, expect } from "@playwright/test";

import { dispatchAgentEvent, openApp } from "./harness.mjs";

const ARTIFACT = {
  plan_id: "PLAN",
  task_id: "task-2",
  summary: "Build the endpoint",
  estimated_impact: "one function",
  ast_targets: [
    {
      file_path: "main.py",
      symbol_name: "",
      byte_range: [0, 0],
      operation: "insert",
      content: "print('hello')\n",
    },
  ],
};

test("the DAG draws one node per milestone and colours it by state", async ({ page }) => {
  await openApp(page);

  // The harness plan has three milestones across two sections; the two phase nodes are not
  // milestones, so they are not counted here.
  await expect(page.locator("#dagFlow .dag-node--task")).toHaveCount(3);

  // Strict state colouring: completed green, pending grey, failed red.
  await expect(page.locator('#dagFlow [data-node-id="task-1"]')).toHaveClass(/dag-node--completed/);
  await expect(page.locator('#dagFlow [data-node-id="task-2"]')).toHaveClass(/dag-node--pending/);
  await expect(page.locator('#dagFlow [data-node-id="task-3"]')).toHaveClass(/dag-node--failed/);

  // The state is written on the node too, so the surface never depends on colour alone.
  await expect(page.locator('#dagFlow [data-node-id="task-3"]')).toContainText("FAILED");

  // The harness plan declares no dependencies, so no edge is drawn and the surface says so
  // rather than inferring a chain from document order.
  await expect(page.locator("#dagEdges path")).toHaveCount(0);
  await expect(page.locator(".dag-edge-note")).toContainText("No dependencies are declared");
});

test("the DAG draws an edge for each declared dependency", async ({ page }) => {
  await openApp(page, {
    plan: {
      filename: "PLAN.md",
      exists: true,
      content: "# Plan\n",
      plans: ["PLAN.md"],
      load_error: "",
      tree: [],
      plan_json: {
        version: "1.0",
        plan_file: "PLAN.md",
        title: "Dep",
        sections: [
          {
            id: "sec-1",
            title: "1. S",
            tasks: [
              { id: "task-1", section: "1. S", title: "First", status: "completed", dependencies: [] },
              { id: "task-2", section: "1. S", title: "Second", status: "pending", dependencies: ["task-1"] },
              { id: "task-3", section: "1. S", title: "Third", status: "pending", dependencies: ["task-1", "task-2"] },
            ],
          },
        ],
        // The flat view is what the plan tree renders from, so it is carried in lock-step --
        // the real projection always has both.
        steps: [
          { id: "task-1", section: "1. S", title: "First", status: "completed", dependencies: [] },
          { id: "task-2", section: "1. S", title: "Second", status: "pending", dependencies: ["task-1"] },
          { id: "task-3", section: "1. S", title: "Third", status: "pending", dependencies: ["task-1", "task-2"] },
        ],
      },
    },
  });

  // Three real blockers: task-1->task-2, task-1->task-3, task-2->task-3.
  await expect(page.locator("#dagEdges path")).toHaveCount(3);
  await expect(page.locator(".dag-edge-note")).toHaveCount(0);

  // Topological order: task-1 before task-2 before task-3.
  const ids = await page.locator("#dagFlow .dag-node").evaluateAll(
    (nodes) => nodes.map((node) => node.dataset.nodeId),
  );
  expect(ids).toEqual(["task-1", "task-2", "task-3"]);
});

test("a planned artifact colours its node amber and offers Approve", async ({ page }) => {
  await openApp(page);

  const node = page.locator('#dagFlow [data-node-id="task-2"]');
  await expect(node).not.toHaveClass(/dag-node--planned/);

  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });
  await dispatchAgentEvent(page, {
    type: "task_state_updated",
    plan_id: "PLAN",
    task_id: "task-2",
    status: "planned",
  });

  await expect(node).toHaveClass(/dag-node--planned/);
  await expect(node).toContainText("PLANNED");

  // The Approve control lives in the inspector, not on the node: the node is the graph, the
  // rail is where a single entity is acted on. The halt already selected this node, so its
  // control is up without a click.
  await expect(page.locator("#inspectorActions").getByRole("button", { name: "Approve" }))
    .toBeVisible();

  // No other node was given an Approve control: the gate is per-milestone.
  await expect(page.locator("#dagFlow .dag-approve-btn")).toHaveCount(0);
});

test("a task_state_updated event repaints the node it names", async ({ page }) => {
  await openApp(page);

  const node = page.locator('#dagFlow [data-node-id="task-2"]');

  await dispatchAgentEvent(page, {
    type: "task_state_updated",
    plan_id: "PLAN",
    task_id: "task-2",
    status: "in_progress",
  });
  await expect(node).toHaveClass(/dag-node--in_progress/);

  await dispatchAgentEvent(page, {
    type: "task_state_updated",
    plan_id: "PLAN",
    task_id: "task-2",
    status: "completed",
  });
  await expect(node).toHaveClass(/dag-node--completed/);
});

test("selecting a planned milestone shows its artifact against the bytes at its span", async ({ page }) => {
  await openApp(page, {
    api: {
      get_source_span: {
        success: true,
        found: true,
        file_path: "main.py",
        start: 0,
        end: 0,
        length: 0,
        text: "",
      },
    },
  });

  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });

  // The halt selects the milestone and opens the inspector, which renders that one node's
  // payload -- no click needed, and no list of every other milestone's artifacts.
  await expect(page.locator("#inspectorNode")).toBeVisible();

  const surface = page.locator("#dagDiffSurface");
  // The operation is stated, and only the artifact's own span is shown -- never a whole file.
  await expect(surface).toContainText("INSERT");
  await expect(surface).toContainText("main.py");
  await expect(surface).toContainText("bytes 0..0");
  await expect(surface).toContainText("print('hello')");
  await expect(surface).toContainText("Build the endpoint");
});

test("a milestone with no artifact says so rather than rendering an empty diff", async ({ page }) => {
  await openApp(page);

  await page.locator("#btnWorkbenchDagView").click();
  await expect(page.locator("#workbenchDagView")).toBeVisible();
  await page.locator('#dagFlow [data-node-id="task-1"]').click();

  // The inspector is showing this one node, and it has no artifact yet.
  await expect(page.locator("#inspectorNode")).toBeVisible();
  await expect(page.locator("#dagDiffSurface")).toContainText("No proposed artifact");
});

test("concurrent milestones share a layer column", async ({ page }) => {
  const task = (id, deps) => ({ id, section: "1. S", title: id, status: "pending", dependencies: deps });
  await openApp(page, {
    plan: {
      filename: "PLAN.md",
      exists: true,
      content: "# Plan\n",
      plans: ["PLAN.md"],
      load_error: "",
      tree: [],
      plan_json: {
        version: "1.0",
        plan_file: "PLAN.md",
        title: "Swarm",
        sections: [
          { id: "sec-1", title: "1. S", tasks: [
            task("task-1", []), task("task-2", []),
            task("task-3", ["task-1"]), task("task-4", ["task-2"]),
          ] },
        ],
        steps: [task("task-1", []), task("task-2", []), task("task-3", ["task-1"]), task("task-4", ["task-2"])],
      },
    },
  });

  // Two roots with no blockers stack in layer 0; their dependents stack in layer 1. A single
  // row would have implied the roots were sequential, which is the lie this layout removes.
  const layers = page.locator("#dagFlow .dag-layer");
  await expect(layers).toHaveCount(2);
  await expect(layers.nth(0).locator(".dag-node")).toHaveCount(2);
  await expect(layers.nth(1).locator(".dag-node")).toHaveCount(2);
  await expect(page.locator("#dagEdges path")).toHaveCount(2);
});

test("the Approve control calls approve_artifact and reports the result", async ({ page }) => {
  await openApp(page, {
    api: {
      approve_artifact: {
        success: true,
        dispatched: true,
        plan_id: "PLAN",
        task_id: "task-2",
        status: "in_progress",
      },
    },
  });

  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });
  // The halt selects the node and opens the inspector, so its Approve control is already up.
  await expect(page.locator("#dagPanel")).toBeVisible();
  await page.locator("#inspectorActions").getByRole("button", { name: "Approve" }).click();

  // The handler reports the outcome; it does not assume the state change, which arrives as
  // its own task_state_updated event.
  await expect(page.locator("#toastContainer")).toContainText("Artifact approved");
});

test("an approval names the run it releases with the tracked intent id", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: "intent-run-7", status: "queued", position: 0 },
      approve_artifact: { success: true, dispatched: true, plan_id: "PLAN", task_id: "task-2" },
    },
  });

  // A real launch, so the engine answers with the execution id the UI must remember. A run
  // replaces the plan workbench (which holds the DAG rail) with the execution stage, so the rail
  // only returns once the run is over and its cards are closed -- the same path a real session
  // takes before a person acts on the halt.
  await page.keyboard.press("Control+k");
  await page.fill("#commandPaletteInput", "next");
  await page.keyboard.press("Enter");
  await page.locator("#btnConfirmActionParam").click();
  await expect.poll(() =>
    page.evaluate(() => (window.__alethFetchBodies || []).some((body) => body && body.action_type))
  ).toBe(true);

  await dispatchAgentEvent(page, { type: "architect_spawn", agent: "software-architect", name: "Lead" });
  await dispatchAgentEvent(page, { type: "workflow_complete", status: "stopped" });
  await page.locator("#btnCloseArchitect").click();
  await expect(page.locator("#emptyStateContainer")).toBeVisible();

  // The halt, and its Approve control.
  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });
  await expect(page.locator("#dagPanel")).toBeVisible();
  await page.locator("#inspectorActions").getByRole("button", { name: "Approve" }).click();

  // The approval must name the run it is releasing: its write lands in that run's shadow (Phase
  // 21), and a shadow with no id is a change no one can merge.
  await expect.poll(() =>
    page.evaluate(() =>
      (window.__alethFetchBodies || []).some(
        (body) => body && body.task_id && body.intent_id === "intent-run-7"
      )
    )
  ).toBe(true);
});

test("a planned target's proposed content is editable before approval", async ({ page }) => {
  await openApp(page, {
    api: {
      get_source_span: { success: true, found: true, file_path: "main.py", start: 0, end: 0, length: 0, text: "" },
      update_artifact_target: { success: true },
    },
  });

  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });

  // The proposed content is offered for amendment, so a hallucinated character costs an edit
  // rather than a rejected run.
  await page.locator(".diff-edit-btn").click();
  const editor = page.locator(".diff-editor");
  await expect(editor).toBeVisible();
  await editor.fill("print('edited')\n");
  await page.locator(".diff-save-btn").click();

  await expect(page.locator("#toastContainer")).toContainText("Artifact target updated");
});

test("rejecting an artifact demands a critique and re-queues the milestone", async ({ page }) => {
  await openApp(page, {
    api: {
      get_source_span: { success: true, found: true, file_path: "main.py", start: 0, end: 0, length: 0, text: "" },
      reject_artifact: { success: true, task_id: "task-2", plan_id: "PLAN", dispatched: ["task-2"] },
    },
  });

  // A planned artifact selects its node and opens the inspector, so the actions are up already.
  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });

  const inspector = page.locator("#dagPanel");
  await expect(inspector).toBeVisible();

  const feedback = page.locator("#reject-feedback");
  const reject = page.getByRole("button", { name: "Reject artifact" });
  await expect(feedback).toBeVisible();

  // A blind rejection would re-queue the node with no new information, so the click cannot happen
  // at all until there is a critique. The backend refuses it too; this is the affordance.
  await expect(reject).toBeDisabled();

  await feedback.fill("the endpoint needs a return type");
  await expect(reject).toBeEnabled();

  await reject.click();

  await expect(page.locator("#toastContainer")).toContainText("Artifact rejected");
  // The inspector closes and the reviewer's words are not carried to the next node they open.
  await expect(inspector).toBeHidden();
  await expect(feedback).toHaveValue("");

  // The milestone leaves PLANNED, back in the queue the swarm dispatches from.
  await expect(page.locator('#dagFlow [data-node-id="task-2"]')).toHaveClass(/dag-node--pending/);
});

test("a halted plan opens the DAG rail", async ({ page }) => {
  await openApp(page);

  // Closed until asked for: the review surface has a rail of its own now, rather than
  // sitting on top of the plan tree inside the workbench.
  await expect(page.locator("#dagPanel")).toBeHidden();
  await expect(page.locator("#btnDagPanel")).toHaveAttribute("aria-expanded", "false");

  await dispatchAgentEvent(page, { type: "artifact_planned", artifact: ARTIFACT });

  // The halt is the moment the review surface matters, so it opens itself.
  await expect(page.locator("#dagPanel")).toBeVisible();
  await expect(page.locator("#btnDagPanel")).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#dagFlow .dag-node--planned")).toHaveCount(1);

  // It is closed from its own header, exactly as the audit rail is: the panel covers the
  // rail that holds its trigger, so the X is the way back.
  await page.locator("#btnCloseDagPanel").click();
  await expect(page.locator("#dagPanel")).toBeHidden();
  await expect(page.locator("#btnDagPanel")).toHaveAttribute("aria-expanded", "false");
});
