// ui/js/dock.js — Bottom dock resize behaviour.
// Self-contained on purpose: one drag gesture on one handle, and nothing else in
// the frontend reads or writes the dock height.
import { DOM } from "./store.js";

export function initDockResize() {
  const dock = DOM.bottomDock;
  const handle = DOM.dockResizeHandle;
  if (!dock || !handle) return;

  // The dock head holds the command drawer now (the status row moved to the top bar), and it
  // is out of the flow whenever the drawer is closed, so there is no head height to clear:
  // 120 leaves a usable handful of transcript lines above the floor. The ceiling is a share of
  // the *viewport*: the transcript is a glanceable companion to the centre stage, not a
  // replacement for it, so it may take about a third of the window before the plan it is
  // describing stops fitting.
  const MIN_HEIGHT = 120;
  const maxHeight = () => {
    return Math.max(MIN_HEIGHT, Math.round(window.innerHeight * 0.3));
  };

  let startY = 0;
  let startHeight = 0;
  let dragging = false;

  const onMove = (event) => {
    if (!dragging) return;
    // The handle sits on the dock's top edge, so dragging up grows the dock.
    const next = startHeight + (startY - event.clientY);
    dock.style.height = Math.min(maxHeight(), Math.max(MIN_HEIGHT, next)) + "px";
    event.preventDefault();
  };

  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    dock.classList.remove("resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", stopDragging);
  };

  handle.addEventListener("pointerdown", (event) => {
    dragging = true;
    startY = event.clientY;
    startHeight = dock.getBoundingClientRect().height;
    dock.classList.add("resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stopDragging);
    event.preventDefault();
  });
}

// ----------------------------------------------------------------------------
// Docked command drawer
// ----------------------------------------------------------------------------
// The drawer is a pane of this dock, so whether one is open is dock state. The class
// below raises the dock's floor (see .bottom-dock.drawer-open in ui/css/dock.css) and
// marks the toolbar button the drawer belongs to, which is what makes the panel read as
// an expansion of that button rather than a view that floated in from elsewhere.
// ui/js/actions.js decides when a drawer opens; this file owns what that looks like.
export function setDockDrawerOpen(open, actionType) {
  const dock = DOM.bottomDock;
  if (!dock) return;

  if (!open) {
    dock.classList.remove("drawer-open");
    return;
  }
  // There is no toolbar button to mark any more: the palette closes on selection, so the
  // drawer opening is itself the acknowledgement that a command was chosen.
  dock.classList.add("drawer-open");
}

// Read by ui/js/actions.js so a second click on the button whose form is already open
// folds the drawer away instead of resetting the fields that were typed into it.
export function isDockDrawerOpen() {
  return !!(DOM.bottomDock && DOM.bottomDock.classList.contains("drawer-open"));
}
