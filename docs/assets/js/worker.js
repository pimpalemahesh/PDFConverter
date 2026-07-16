/* worker.js — runs Pyodide + the pdf2epub Python engine off the main thread.
 *
 * Loads a PyMuPDF WebAssembly wheel (built in CI, see .github/workflows/deploy.yml)
 * via loadPackage() — micropip cannot install PyMuPDF because of its shared libs.
 * The runtime Pyodide version below must share the wheel's ABI (2024_0).
 */
const PYODIDE_VERSION = "0.27.2";
importScripts(
  `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/pyodide.js`,
);

// Resolve URLs against the site root, not this script's /assets/js/ location,
// so the app works whether hosted at a domain root or a project-pages subpath.
const ROOT = new URL("../../", self.location.href);

let pyodide = null;
let ready = false;

const send = (type, extra = {}) => self.postMessage({ type, ...extra });

async function init() {
  send("status", { message: `Loading Pyodide ${PYODIDE_VERSION}…` });
  pyodide = await loadPyodide({
    indexURL: `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`,
  });

  send("status", { message: "Loading PDF engine (PyMuPDF · WebAssembly)…" });
  let manifest;
  try {
    const res = await fetch(new URL("vendor/manifest.json", ROOT), {
      cache: "no-cache",
    });
    manifest = await res.json();
  } catch (e) {
    throw new Error(
      "Could not read vendor/manifest.json — the PyMuPDF wheel has not been built. " +
        "Run the GitHub Actions deploy workflow (it builds the wheel), or build one locally into docs/vendor/.",
    );
  }
  if (!manifest.pymupdf) {
    throw new Error(
      "No PyMuPDF wheel listed in vendor/manifest.json. " +
        "The deploy workflow builds it in CI; the app runs once that has completed.",
    );
  }
  await pyodide.loadPackage(new URL(`vendor/${manifest.pymupdf}`, ROOT).href);

  send("status", { message: "Loading converter…" });
  const src = await (
    await fetch(new URL("engine/pdf2epub/converter.py", ROOT), {
      cache: "no-cache",
    })
  ).text();
  pyodide.FS.mkdirTree("/pkg/pdf2epub");
  pyodide.FS.writeFile("/pkg/pdf2epub/converter.py", src);
  pyodide.FS.writeFile(
    "/pkg/pdf2epub/__init__.py",
    "from .converter import run\n",
  );
  await pyodide.runPythonAsync(`
import sys
if "/pkg" not in sys.path:
    sys.path.insert(0, "/pkg")
import pdf2epub
`);

  ready = true;
  send("ready", { pymupdf: manifest.pymupdf, pyodide: PYODIDE_VERSION });
}

const initPromise = init().catch((err) =>
  send("error", { message: String(err.message || err) }),
);

self.onmessage = async (e) => {
  const msg = e.data || {};
  if (msg.type !== "convert") return;

  await initPromise;
  if (!ready) {
    send("error", { message: "Engine failed to initialize." });
    return;
  }

  try {
    pyodide.FS.mkdirTree("/work");
    pyodide.FS.writeFile("/work/in.pdf", new Uint8Array(msg.bytes));

    pyodide.globals.set("_js_progress", (m) =>
      send("progress", { message: m }),
    );
    pyodide.globals.set("_in_title", msg.title || null);
    pyodide.globals.set("_in_author", msg.author || null);

    const statsJson = await pyodide.runPythonAsync(`
import json, pdf2epub
_stats = pdf2epub.run("/work/in.pdf", "/work/out.epub",
                      title=_in_title, author=_in_author,
                      progress=lambda m: _js_progress(m))
json.dumps(_stats)
`);

    const out = pyodide.FS.readFile("/work/out.epub"); // Uint8Array
    try {
      pyodide.FS.unlink("/work/in.pdf");
      pyodide.FS.unlink("/work/out.epub");
    } catch (_) {}

    const buf = out.buffer.slice(0); // own copy we can transfer
    self.postMessage(
      {
        type: "done",
        stats: JSON.parse(statsJson),
        epub: buf,
        filename: msg.filename,
      },
      [buf],
    );
  } catch (err) {
    send("error", { message: String(err.message || err) });
  }
};
