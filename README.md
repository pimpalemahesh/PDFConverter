# PDF → EPUB Converter

A small Flask web app that converts text-based PDFs into clean, **reflowable
EPUB 3** files that render properly in Apple Books — justified and de-hyphenated
paragraphs, styled code blocks, real HTML tables, extracted figures, a navigable
table of contents, and dark-mode-safe styling.

Rather than hardcoding one book's fonts, the engine **profiles each PDF** — it
learns the body text size, the text margin, and the running header/footer text —
then classifies content by *relative* font metrics, so it works across PDFs from
different publishers and tools (FrameMaker, Word/Distiller, etc.).

## Setup

```bash
cd ~/code/projects/PDFConverter
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Run the server

```bash
./venv/bin/python app.py
# then open http://127.0.0.1:5000
```

Drop a PDF onto the page, optionally set a title/author, and click **Convert**.
The finished `.epub` appears in the "Converted books" list; downloaded files are
also kept in `outputs/`. To open one in Apple Books, download it and double-click,
or drag it into the Books app.

## Command line

The engine also runs standalone:

```bash
./venv/bin/python converter.py input.pdf output.epub --title "My Book" --author "Someone"
```

## How it works

`converter.py` reads the PDF with PyMuPDF, profiles it, then classifies content
by relative font metrics to reconstruct document structure:

- **Body text** is re-flowed into real paragraphs, removing the PDF's hard line
  breaks and soft (line-break) hyphenation. Genuine compounds like `real-time`
  are preserved by scanning the whole book for compounds that appear hyphenated
  *mid-line* — a line-break hyphen is only kept if the compound is attested
  elsewhere, which is far more reliable than a dictionary lookup.
- **Code** is detected as *monospace-dominant* blocks that actually look like
  code (multi-line or containing code punctuation), so lone identifiers aren't
  boxed. Rendered as wrap-friendly `<pre>` with indentation measured from glyph
  advances.
- **Headings** are matched against the PDF's own table of contents
  (font-independent), with a bold/large-size fallback; they map to
  `<h1>`/`<h2>`/`<h3>` and feed the EPUB navigation. The chapter level is chosen
  automatically, and the book's own numbering is respected (no double numbering).
- **Figures** come from both **embedded raster images** and **vector line-art
  clusters**, rendered as clipped PNGs. Decorative/repeated icons, page frames,
  and text-dense regions (code, prose, indexes) are filtered out so only real
  diagrams are captured. `Figure N`/`Table N` captions are attached automatically.
- **Tables** are reconstructed as HTML tables by clustering cell x-positions.
- **Definition lists, footnotes, and bullet lists** get their own styling.
- Running headers/footers, page numbers, and the print index are dropped.

Output is validated for XML well-formedness and correct EPUB packaging
(`mimetype` stored first and uncompressed, resolvable nav anchors, no duplicate
ids).

## Scope / limitations

Works on digitally-produced (not scanned) single- and two-column PDFs. Verified
end-to-end on two structurally different books — a FrameMaker O'Reilly title and
a Word/Distiller title. Known limits:

- **Scanned/image-only PDFs won't produce text** — there is no OCR step (though
  their page images would still be captured).
- Very complex magazine-style layouts may reflow imperfectly.
- Occasionally a decorative note/callout box drawn as vector art can be captured
  as a figure (it still renders its real content); real captioned figures are
  detected reliably.
- PDFs without an embedded outline fall back to a single-document EPUB.

## Layout

```
converter.py        conversion engine (importable + CLI)
app.py              Flask server
templates/index.html  upload UI
uploads/            transient upload storage (auto-cleaned per request)
outputs/            generated .epub files
```
