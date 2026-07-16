# PDF → EPUB (browser edition)

Convert text-based PDFs into clean, **reflowable EPUB 3** files that render
properly in Apple Books — **entirely in the browser**. No server, no upload:
the PDF is processed on the user's own device with Python compiled to
WebAssembly. Deployable as a static site to GitHub Pages.

**🔗 Live site:** https://pimpalemahesh.github.io/PDFConverter/
&nbsp;·&nbsp; local preview: `python3 -m http.server -d docs 8000` → http://localhost:8000

> The live site goes up once this repo is pushed and **Settings → Pages →
> Source** is set to **GitHub Actions**.

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
        │  loadPyodide()          ── Pyodide CDN
        │  loadPackage("pymupdf") ── bundled in Pyodide 0.28.1+
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
- **PyMuPDF** ships inside Pyodide's own distribution as of **0.28.1**, so it
  loads by name with `loadPackage("pymupdf")` straight from the Pyodide CDN — no
  build step, no self-hosted wheel, no `micropip` (which can't install it because
  of its shared libraries). `PYODIDE_VERSION` in `docs/assets/js/worker.js` is
  pinned to a release that bundles PyMuPDF.

There is nothing to compile: the whole app is static files.

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
│   └── engine/pdf2epub/           # the Python engine (served to Pyodide)
│       ├── __init__.py
│       └── converter.py
├── .github/workflows/deploy.yml   # publish docs/ to Pages (no build step)
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
3. The `deploy.yml` workflow runs automatically and publishes `docs/` (no build
   step — it finishes in under a minute).
4. Open the Pages URL. The engine warms up on load; drop in a PDF and convert.

```bash
git add -A && git commit -m "Browser-based PDF→EPUB converter"
git push
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

# preview the full web UI (fetches Pyodide + PyMuPDF from the CDN on first load)
python3 -m http.server -d docs 8000     # → http://localhost:8000
```

The web preview works out of the box — Pyodide and PyMuPDF are fetched from the
CDN at runtime, so there is nothing to build or vendor locally.

---

## Verification status

Validated end-to-end, locally:

- Engine tests pass under CPython (`pytest`), including a real-book check.
- The **full browser pipeline** was exercised under **real Pyodide 0.28.2** (the
  same runtime the site uses): `loadPackage("pymupdf")` from the bundled
  distribution, `import pdf2epub`, then a complete conversion of a 385-page book
  to a valid EPUB (correct chapters, figures, and OCF packaging).
- JS syntax-checked; workflow YAML validated; `black` + `prettier` clean; `ruff`
  reports no issues.

To move to a newer Pyodide, bump `PYODIDE_VERSION` in `worker.js` to any release
that bundles `pymupdf` (0.28.1+).

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
