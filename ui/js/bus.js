// ui/js/bus.js — The smallest possible strict pub/sub.
//
// A leaf module: it imports nothing from ui/js and holds no state beyond the listener
// table. It exists so a module that would otherwise have to import a *caller* can publish
// an intent instead. A feature renderer that needs to open the action drawer used to import
// `actions.js`; that back-edge is what made the import graph a strongly-connected component.
// With the bus the renderer emits `action:open` and the composition layer (ui/js/wire.js)
// subscribes and maps it to the real implementation, so the module dependency points down
// and the graph stays acyclic.
//
// There is deliberately nothing else here: no globals, no wildcard/`*` channel, no state.
// Subscriptions are registered by the composition layer at module top level, so they exist
// before any event can fire.

const listeners = new Map();

/** Subscribes `fn` to `type`. Returns an unsubscribe function. */
export function on(type, fn) {
  if (typeof type !== "string" || typeof fn !== "function") return () => {};
  let bucket = listeners.get(type);
  if (!bucket) {
    bucket = new Set();
    listeners.set(type, bucket);
  }
  bucket.add(fn);
  return () => off(type, fn);
}

/** Removes a subscription made with `on`. */
export function off(type, fn) {
  const bucket = listeners.get(type);
  if (!bucket) return;
  bucket.delete(fn);
  if (bucket.size === 0) listeners.delete(type);
}

/** Publishes `payload` to every listener of `type`. Unhandled types are a no-op. */
export function emit(type, payload) {
  const bucket = listeners.get(type);
  if (!bucket) return;
  // Snapshot before dispatch, so a listener that subscribes or unsubscribes while
  // responding cannot mutate the set mid-iteration.
  for (const fn of Array.from(bucket)) fn(payload);
}
