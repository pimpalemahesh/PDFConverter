# PDF → EPUB (browser edition)

Convert text-based PDFs into clean, **reflowable EPUB 3** files that render
properly in Apple Books — **entirely in the browser**. No server, no upload:
the PDF is processed on the user's own device with Python compiled to
WebAssembly. Deployable as a static site to GitHub Pages.

**🔗 Live site:** https://pimpalemahesh.github.io/PDFConverter/
&nbsp;·&nbsp; local preview: `python3 -m http.server -d docs 8000` → http://localhost:8000

> The live site goes up once this repo is pushed and **Settings → Pages →
> Source** is set to **GitHub Actions** (the deploy workflow builds the wheel and
> publishes it).

Justified, de-hyphenated paragraphs · styled code blocks · real tables ·
extracted figures (raster + vector) · a navigable table of contents ·
dark-mode-safe styling.

---

## How it works

```
Browser (main thread, app.js)
        │  file bytes ─ transfer ─▶
        ▼
Web Worker (worker.js)
        │  loadPyodide()  ── CDN
        │  loadPackage(vendor/PyMuPDF-…-wasm32.whl)   ← built in CI
        │  import pdf2epub  (engine/pdf2epub/converter.py)
        ▼
pdf2epub.run(in.pdf → out.epub)   ← the exact same Python engine used by cli.py
        │  EPUB bytes ─ transfer ─▶
        ▼
Browser triggers download
```

- **`docs/`** is the entire static site (what GitHub Pages serves).
- **`docs/engine/pdf2epub/converter.py`** is the conversion engine. The same
  module runs under CPython (tests, `cli.py`) and under Pyodide (the browser),
  because it depends only on **PyMuPDF + the Python standard library**.
- **Pyodide** runs Python in the browser via WebAssembly, inside a **Web Worker**
  so the UI never freezes and progress can stream live.
- **PyMuPDF** can't be installed with `micropip` (it uses shared libraries), and
  no prebuilt Pyodide wheel is published — so CI **compiles one** with
  `cibuildwheel` (mirroring PyMuPDF's own tested Pyodide build) and drops it into
  `docs/vendor/`, where `loadPackage()` loads it at runtime (same-origin, no CORS).

The runtime Pyodide version (`PYODIDE_VERSION` in `docs/assets/js/worker.js`) and
the wheel's ABI (`2024_0`) must match; both are pinned to Pyodide **0.27.x**.

---

## Project layout

```
PDFConverter/
├── docs/                          # ← GitHub Pages root (static site)
│   ├── index.html
│   ├── .nojekyll
│   ├── assets/
│   │   ├── css/style.css
│   │   └── js/
│   │       ├── app.js             # UI controller (main thread)
│   │       └── worker.js          # Pyodide runtime (Web Worker)
│   ├── engine/pdf2epub/           # the Python engine (served to Pyodide)
│   │   ├── __init__.py
│   │   └── converter.py
│   └── vendor/                    # PyMuPDF wheel + manifest (wheel built by CI)
│       └── manifest.json
├── .github/workflows/deploy.yml   # build wheel + deploy to Pages
├── tests/test_engine.py           # engine tests (CPython)
├── cli.py                         # local command-line entry point
├── requirements-dev.txt           # dev/test deps only
├── pyproject.toml
└── LICENSE
```

---

## Deploy to GitHub Pages

1. Push this repository to GitHub (branch `main`).
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. The `deploy.yml` workflow runs automatically. Its first run builds the
   PyMuPDF WebAssembly wheel (~20–30 min; cached afterwards), then publishes
   `docs/`. Subsequent deploys reuse the cached wheel and take under a minute.
4. Open the Pages URL. The engine warms up on load; drop in a PDF and convert.

```bash
git add -A && git commit -m "Browser-based PDF→EPUB converter"
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main
```

---

## Local development

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements-dev.txt

# run the engine tests (CPython)
./venv/bin/python -m pytest

# convert on the command line
./venv/bin/python cli.py input.pdf output.epub --title "My Book"

# preview the web UI (engine runs once a wheel is present in docs/vendor/)
python3 -m http.server -d docs 8000     # → http://localhost:8000
```

To exercise the **full browser pipeline locally** you need a PyMuPDF Pyodide
wheel in `docs/vendor/` and its filename in `manifest.json`. That wheel is a CI
artifact (building it requires an Emscripten toolchain), so the usual path is to
let the GitHub Actions workflow build it; the deployed site is the reference
environment.

---

## Verification status

Validated locally:

- Engine tests pass under CPython (`pytest`), including a real-book check.
- Under **real Pyodide 0.27.2**: the engine compiles, every stdlib import it uses
  resolves, and `import pdf2epub` is blocked *only* by `fitz` — i.e. the code is
  Pyodide-clean and will import as soon as the wheel loads.
- `loadPackage(url)` (the wheel-loading mechanism) verified against Pyodide's CDN.
- JS syntax-checked; workflow YAML and manifest JSON validated.

Completed by CI on first deploy: the Emscripten wheel build and the in-browser
`import fitz`. If that build ever needs tuning, the version pins live at the top
of `deploy.yml` (`PYMUPDF_REF`, `MUPDF_BUILD`, `CIBW_VERSION`) and must stay
ABI-compatible with `PYODIDE_VERSION` in `worker.js`.

---

## Scope / limitations

Works on digitally-produced (not scanned) single- and two-column PDFs. Verified
on two structurally different books (a FrameMaker O'Reilly title and a
Word/Distiller title).

- **Scanned / image-only PDFs** yield no text — there is no OCR step.
- Very complex magazine-style layouts may reflow imperfectly.
- Occasionally a decorative callout box drawn as vector art is captured as a
  figure (it still shows its real content); captioned figures are reliable.
- PDFs without an embedded outline fall back to a single-document EPUB.
