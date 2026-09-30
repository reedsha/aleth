// dependency-cruiser configuration for the frontend module graph.
//
// `eslint-plugin-import-x`'s `no-cycle` rule (see eslint.config.mjs) and this rule check the
// same property from two directions. ESLint is the fast, in-editor gate; dependency-cruiser
// is the one wired into `npm run lint:deps`, and it is the one whose output states the whole
// graph, so a cycle is named rather than merely failed.
//
// One rule only: `no-circular` at error. A cycle in `ui/js` means a module's imports point
// back up at a caller, which makes evaluation order something the app cannot state and is
// exactly what the bus split (ui/js/bus.js) exists to remove. Break the back-edge through
// the bus; do not weaken this rule.

/** @type {import("dependency-cruiser").IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: "no-circular",
      severity: "error",
      comment:
        "A cycle in ui/js (a back-edge to a caller). Publish an intent on ui/js/bus.js " +
        "and let ui/js/wire.js map it, so the import edge points down.",
      from: {},
      to: { circular: true },
    },
  ],
  options: {
    // node_modules is not part of the app graph.
    doNotFollow: { path: "node_modules" },
    // The stylesheet imports in main.js are Vite's, not module edges; excluding them keeps
    // the summary about the JS graph the rule is actually about.
    exclude: { path: "\\.css$" },
    tsPreCompilationDeps: false,
    reporterOptions: {
      dot: {
        collapsePattern: "node_modules/[^/]+",
      },
    },
  },
};
