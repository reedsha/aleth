// ESLint for the frontend sources.
//
// This config exists to catch *correctness* problems in `ui/js`, not to impose a style:
// the rules below are the ones that would have caught the two faults the ES-module
// migration actually produced -- an identifier used without being imported, and a name
// declared twice in one module.
//
// It is only possible at all because the modules are ES modules now. While they were
// classic scripts sharing one global scope, `no-undef` fired on nearly every line (every
// cross-file reference was an undeclared global), so a linter could say nothing useful
// about them. Explicit imports are what make the rule meaningful.
//
// The graph rule below is the other half of that: explicit imports are only useful if the
// graph they form is a DAG. `import-x/no-cycle` fails the build on any cycle, so a new one
// cannot land without the lint gate naming it. The import graph is also checked by
// dependency-cruiser (`.dependency-cruiser.cjs`, run by `npm run lint:deps`), which sees
// edges through dynamic imports and non-JS targets that this rule does not.
//
// `eslint-plugin-import-x` (the maintained fork of `eslint-plugin-import`) is used because
// it declares support for eslint 10; the original caps its peer range at eslint 9, which
// forced a `legacy-peer-deps` bypass that is no longer needed.

import globals from "globals";
import js from "@eslint/js";
import importX from "eslint-plugin-import-x";

export default [
  {
    files: ["ui/js/**/*.js"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        // Published by the document head before any module runs (ui/index.html), and read
        // by the modules so every failure lands in the same stack of banners.
        __deepAgentsReport: "readonly",
      },
    },
    plugins: {
      "import-x": importX,
    },
    rules: {
      ...js.configs.recommended.rules,
      // The rule that matters most here: a reference to a name that is neither imported,
      // declared, nor a browser global. That is the exact shape of the migration bug that
      // left `state` undefined in five result renderers.
      "no-undef": "error",
      "no-redeclare": "error",
      // A cycle in `ui/js` makes module evaluation order the app cannot state, which is the
      // exact fault the bus split (ui/js/bus.js) removed: break the back-edge through the
      // bus, do not silence this rule.
      "import-x/no-cycle": ["error", { maxDepth: Infinity }],
      // Unused *variables* are worth knowing about; unused function arguments are not --
      // several handlers take a signature they do not all use, and `_`-prefixed names are
      // an established convention in these files (including `catch (_err)` where the
      // handler has nothing to say about the error).
      "no-unused-vars": [
        "error",
        { args: "none", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      // `==`/`!=` are used deliberately in a few places against DOM values; the strict
      // forms are not worth a rewrite of working code.
      eqeqeq: "off",
    },
  },
];
