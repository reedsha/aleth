// ui/js/dock.js — Bottom dock resize behaviour.
// Self-contained on purpose: one drag gesture on one handle, and nothing else in
// the frontend reads or writes the dock height.
function initDockResize() {
  const dock = DOM.bottomDock;
  const handle = DOM.dockResizeHandle;
  if (!dock || !handle) return;

  // The action toolbar at the dock's head is ~146px tall, so 160 is the floor that
  // keeps it fully visible. The ceiling is a share of the pane the dock shares with
  // the centre stage rather than of the window, which also counts the 48px top bar,
  // so the workbench always keeps a visible share of the box being divided.
  const MIN_HEIGHT = 160;
  const maxHeight = () => {
    const pane = dock.parentElement;
    const available = pane ? pane.clientHeight : window.innerHeight;
    return Math.max(MIN_HEIGHT, Math.round(available * 0.6));
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
function setDockDrawerOpen(open, actionType) {
  const dock = DOM.bottomDock;
  if (!dock) return;

  // Only ever one form is open, so the mark moves rather than accumulates.
  const marked = document.querySelectorAll(".action-btn.active");
  for (let i = 0; i < marked.length; i++) {
    marked[i].classList.remove("active");
  }

  if (!open) {
    dock.classList.remove("drawer-open");
    return;
  }

  const btn = actionType
    ? document.querySelector('.action-btn[data-action="' + actionType + '"]')
    : null;
  if (btn) btn.classList.add("active");
  dock.classList.add("drawer-open");
}

// Read by ui/js/actions.js so a second click on the button whose form is already open
// folds the drawer away instead of resetting the fields that were typed into it.
function isDockDrawerOpen() {
  return !!(DOM.bottomDock && DOM.bottomDock.classList.contains("drawer-open"));
}
