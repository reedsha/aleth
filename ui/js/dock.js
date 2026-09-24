// ui/js/dock.js — Bottom dock resize behaviour.
// Self-contained on purpose: one drag gesture on one handle, and nothing else in
// the frontend reads or writes the dock height.
function initDockResize() {
  const dock = DOM.bottomDock;
  const handle = DOM.dockResizeHandle;
  if (!dock || !handle) return;

  // The toolbar at the dock's foot is ~137px tall, so 160 is the floor that keeps
  // it fully visible; the ceiling leaves the center stage usable on short windows.
  const MIN_HEIGHT = 160;
  const maxHeight = () => Math.max(MIN_HEIGHT, Math.round(window.innerHeight * 0.7));

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
