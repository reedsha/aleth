// Vite configuration: `ui/` is the source, `dist/` is the artifact the app serves.
//
// The frontend is ES modules, so there is nothing bespoke here any more: Vite resolves
// `ui/js/main.js` from `ui/index.html`, bundles the module graph, extracts the imported
// stylesheets into one stylesheet, and rewrites the document's asset references to the
// built names. The previous configuration carried a plugin that concatenated ordered
// classic scripts by hand, because the sources shared one global scope and had to be
// loaded in a specific order; explicit imports replaced it.

import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const UI_DIR = fileURLToPath(new URL("./ui", import.meta.url));
const DIST_DIR = fileURLToPath(new URL("./dist", import.meta.url));

export default defineConfig({
  root: UI_DIR,
  base: "/",
  build: {
    outDir: DIST_DIR,
    // `dist/` lives outside `root`, so it has to be emptied explicitly. It is rebuilt from
    // scratch on every build and is gitignored; nothing in it is ever edited by hand.
    emptyOutDir: true,
    // Minified, with source maps. The app is local so the bytes do not matter, but the
    // packaged window runs with `debug=False`, where a stack trace is the difference
    // between "it threw" and "line 1, column 48211". `app.py --debug` opens DevTools
    // against these maps and shows the original module.
    sourcemap: true,
  },
});
