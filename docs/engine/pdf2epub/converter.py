#!/usr/bin/env python3
"""
Generic reflowable EPUB 3 builder for text-based PDFs.

Rather than hardcoding one book's fonts, the engine profiles each PDF to learn
its body size, text margin, and running-header/footer text, then classifies
content by *relative* font metrics:

  * code      -> monospace font family (name-based; the PDF mono flag is unreliable)
  * headings  -> matched against the PDF's own table of contents (font-independent),
                 with a bold/large-size fallback
  * figures   -> embedded raster images AND vector-drawing clusters, both rendered
                 as clipped PNGs, with decorative/repeated icons filtered out
  * tables    -> caption-anchored column clustering
  * body      -> reflowed paragraphs with dictionary-checked de-hyphenation

Works best on digitally-produced (not scanned) single- or two-column PDFs.
Scanned/image-only PDFs have no extractable text (no OCR step).

Usage:
    python converter.py INPUT.pdf OUTPUT.epub [--title "..."] [--author "..."]
"""

import argparse
import collections
import hashlib
import html
import os
import re
import shutil
import statistics
import tempfile
import zipfile
import xml.etree.ElementTree as ET

import fitz

# ---- per-run state (set by run(); one conversion per process) ----
doc = None
DICT = set()
BUILD = OEBPS = None
META_TITLE = "Untitled"
META_AUTHOR = "Unknown"
UID = "urn:uuid:00000000-0000-0000-0000-000000000000"
MODIFIED = "2020-01-01T00:00:00Z"

# document profile (filled by profile_document)
BODY_SIZE = 11.0
BODY_LEFT = 72.0
PAGE_W = 612.0
PAGE_H = 792.0
CHROME = set()  # normalized header/footer strings
REPEATED_XREFS = set()  # image xrefs that repeat across pages (decorative)
KNOWN_HYPHENS = set()  # real compounds seen hyphenated mid-line (e.g. "real-time")
chapters = []
subentries = collections.defaultdict(list)
fig_counter = 0

MONO_HINTS = (
    "mono",
    "courier",
    "consol",
    "menlo",
    "inconsol",
    "typewriter",
    "sourcecode",
    "andalemono",
    "lucidacons",
    "lettergothic",
    "pragmata",
    "dejavusansmono",
    "cousine",
    "ibmplexmono",
    "notosansmono",
    "fixed",
)
BOLD_HINTS = (
    "bold",
    "black",
    "heavy",
    "semibold",
    "demibold",
    "demi",
    "extrab",
    "ultrab",
)
ITAL_HINTS = ("italic", "oblique")

CAP_RE = re.compile(
    r"^\s*(figure|fig\.|table|listing|example|plate|exhibit)\s*[\d]", re.I
)
FRONT_BACK_RE = re.compile(
    r"^\s*(dedication|preface|foreword|acknowledg|about (the|this)|glossary|"
    r"bibliography|colophon|index|contents|table of contents|copyright|"
    r"references|notes|afterword|epilogue|prologue|credits|revision|appendix)\b",
    re.I,
)
SELF_NUM_RE = re.compile(
    r"^\s*(chapter|part|appendix|section|lesson|unit|book)\b|^\s*\d+[.:) ]", re.I
)


# ---------- dictionary for de-hyphenation ----------
def load_dict():
    global DICT
    for path in ("/usr/share/dict/words", "/usr/share/dict/web2"):
        try:
            with open(path) as f:
                DICT = {w.strip().lower() for w in f}
            return
        except OSError:
            continue


# ---------- font role predicates (generic) ----------
def is_mono(font):
    f = font.lower()
    return any(h in f for h in MONO_HINTS)


def is_bold(font, flags=0):
    f = font.lower()
    return any(h in f for h in BOLD_HINTS) or bool(flags & 16)


def is_italic(font, flags=0):
    f = font.lower()
    return any(h in f for h in ITAL_HINTS) or bool(flags & 2)


def norm(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def line_text(spans):
    return "".join(s["text"] for s in spans)


# ---------- block model ----------
class Blk:
    __slots__ = ("x0", "y0", "x1", "y1", "lines")

    def __init__(s, b):
        s.x0, s.y0, s.x1, s.y1 = b["bbox"]
        s.lines = [
            {"bbox": ln["bbox"], "spans": ln["spans"]}
            for ln in b["lines"]
            if ln["spans"]
        ]

    @property
    def text(s):
        return " ".join(line_text(ln["spans"]) for ln in s.lines)

    @property
    def rect(s):
        return fitz.Rect(s.x0, s.y0, s.x1, s.y1)


def block_profile(blk):
    census = collections.Counter()
    for ln in blk.lines:
        for s in ln["spans"]:
            census[(s["font"], round(s["size"], 1), s.get("flags", 0))] += max(
                1, len(s["text"].strip())
            )
    (font, size, flags), _ = census.most_common(1)[0]
    return font, size, flags


def is_chrome(blk):
    """Running header/footer or bare page number."""
    y0, y1 = blk.y0, blk.y1
    top, bottom = PAGE_H * 0.085, PAGE_H * 0.915
    if y1 < top or y0 > bottom:
        t = re.sub(r"\s+", " ", blk.text).strip()
        if not t:
            return True
        if re.fullmatch(r"[\divxlcdmIVXLCDM\-–—.,()\s]+", t):
            return True  # page number / roman numeral
        if norm_chrome(t) in CHROME:
            return True
    return False


def norm_chrome(t):
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", t)).strip().lower()


# ---------- document profiling ----------
def profile_document():
    global BODY_SIZE, BODY_LEFT, PAGE_W, PAGE_H, CHROME, REPEATED_XREFS, KNOWN_HYPHENS
    PAGE_W, PAGE_H = doc[0].rect.width, doc[0].rect.height
    size_hist = collections.Counter()
    left_hist = collections.Counter()
    top_bot = collections.Counter()
    xref_pages = collections.defaultdict(set)
    KNOWN_HYPHENS = set()

    # real compounds appear hyphenated WITHIN a line; a hyphen at a line end is
    # followed by "\n" so this regex never matches soft (break) hyphens.
    for pno in range(doc.page_count):
        for m in re.findall(r"([a-z]{2,}-[a-z]{2,})", doc[pno].get_text().lower()):
            KNOWN_HYPHENS.add(m)

    step = max(1, doc.page_count // 120)
    for pno in range(0, doc.page_count, step):
        page = doc[pno]
        h = page.rect.height
        d = page.get_text("dict")
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            blk = Blk(b)
            if not blk.lines:
                continue
            font, size, flags = block_profile(blk)
            txt = blk.text
            if blk.y1 < h * 0.085 or blk.y0 > h * 0.915:
                nt = norm_chrome(txt)
                if nt:
                    top_bot[nt] += 1
            if is_mono(font):
                continue
            nchars = sum(len(s["text"]) for ln in blk.lines for s in ln["spans"])
            size_hist[round(size * 2) / 2] += nchars
            left_hist[round(blk.x0)] += len(blk.lines)
    for pno in range(doc.page_count):
        for im in doc[pno].get_images(full=True):
            xref_pages[im[0]].add(pno)

    if size_hist:
        BODY_SIZE = max(size_hist, key=size_hist.get)
    if left_hist:
        BODY_LEFT = min((left_hist.most_common(5)), key=lambda kv: kv[0])[0]
    sampled = len(range(0, doc.page_count, step))
    thresh = max(3, sampled * 0.2)
    CHROME = {t for t, c in top_bot.items() if c >= thresh}
    REPEATED_XREFS = {x for x, pages in xref_pages.items() if len(pages) > 3}


# ---------- chapter splitting from the PDF's own TOC ----------
def clean_title(t):
    return re.sub(r"\s+", " ", t).strip()


def build_chapters():
    global chapters, subentries
    chapters, subentries = [], collections.defaultdict(list)
    toc = doc.get_toc()

    if not toc:
        chapters.append(
            {
                "id": "text",
                "disp": META_TITLE,
                "title": META_TITLE,
                "start": 0,
                "end": doc.page_count,
            }
        )
        return

    level_counts = collections.Counter(lvl for lvl, _, _ in toc)
    chap_level = next(
        (L for L in sorted(level_counts) if level_counts[L] >= 3), min(level_counts)
    )
    entries = [(clean_title(t), p - 1) for lvl, t, p in toc if lvl == chap_level]

    self_num = sum(1 for t, _ in entries if SELF_NUM_RE.match(t)) >= 2
    num = 0
    used = set()
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < len(entries) else doc.page_count
        end = max(end, start + 1)
        # a print index / TOC is useless in a reflowable book (page numbers are gone)
        if re.match(
            r"^\s*(index|table of contents|contents|list of (figures|tables))\s*$",
            title,
            re.I,
        ):
            continue
        if self_num or FRONT_BACK_RE.match(title):
            disp = title
        else:
            num += 1
            disp = f"Chapter {num}. {title}"
        cid = "c%02d_%s" % (i, re.sub(r"[^a-z0-9]+", "", title.lower())[:20] or "sec")
        while cid in used:
            cid += "x"
        used.add(cid)
        chapters.append(
            {"id": cid, "disp": disp, "title": title, "start": start, "end": end}
        )

    for lvl, t, p in toc:
        if lvl <= chap_level:
            continue
        for ci, ch in enumerate(chapters):
            if ch["start"] <= p - 1 < ch["end"]:
                subentries[ci].append((lvl - chap_level, clean_title(t), p - 1))
                break


# ---------- image / figure detection ----------
def merge_rects(rects, gap=12):
    rects = [fitz.Rect(r) for r in rects if not fitz.Rect(r).is_empty]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            placed = False
            for i, o in enumerate(out):
                if r.intersects(o) or (r + (-gap, -gap, gap, gap)).intersects(o):
                    out[i] = o | r
                    placed = changed = True
                    break
            if not placed:
                out.append(r)
        rects = out
    return rects


def region_text_chars(page, rect):
    """Count ALL visible text characters whose center lies in rect."""
    n = 0
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            for s in ln["spans"]:
                cx = (s["bbox"][0] + s["bbox"][2]) / 2
                cy = (s["bbox"][1] + s["bbox"][3]) / 2
                if rect.contains(fitz.Point(cx, cy)):
                    n += len(s["text"].strip())
    return n


def image_regions(page):
    """Return merged rectangles that should be rendered as figure images.

    A region is kept only if it is either an embedded raster image or a cluster
    of genuine vector line-art (many drawing primitives with sparse text). This
    deliberately avoids rasterizing text-heavy pages (code, prose, indexes) that
    merely sit inside a page frame or border rectangle.
    """
    pw, ph = page.rect.width, page.rect.height
    parea = pw * ph
    total_chars = len(page.get_text().strip())
    cand = []

    # embedded raster images (kept even without a caption)
    for im in page.get_images(full=True):
        xref = im[0]
        if xref in REPEATED_XREFS:
            continue
        for r in page.get_image_rects(xref):
            if r.width < 0.14 * pw or r.height < 0.045 * ph:
                continue
            if r.get_area() > 0.85 * parea and total_chars > 200:
                continue  # full-page background sitting behind text
            cand.append(fitz.Rect(r))

    # vector drawing primitives
    prims = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.is_empty:
            continue
        w, h = r.width, r.height
        if (h < 3 and w > 0.4 * pw) or (w < 3 and h > 0.4 * ph):
            continue  # ruled line
        if w < 4 or h < 4:
            continue  # tiny stroke
        if w > 0.85 * pw and h > 0.85 * ph:
            continue  # page/content frame rectangle
        prims.append(r)
    for cl in merge_rects(prims, gap=16):
        if (
            cl.width < 0.18 * pw
            or cl.height < 0.05 * ph
            or cl.get_area() < 0.03 * parea
        ):
            continue
        n_prims = sum(1 for r in prims if cl.intersects(r))
        if n_prims < 5:
            continue  # a real diagram is many strokes, not one box
        chars = region_text_chars(page, cl)
        density = chars / (cl.get_area() / 1000.0)
        if chars > 140 or density > 2.5:
            continue  # text-dense -> prose/code/table, not a diagram
        cand.append(cl)

    out = []
    for r in merge_rects(cand, gap=10):
        r = r & page.rect
        if r.width < 0.14 * pw or r.height < 0.045 * ph:
            continue
        if r.get_area() > 0.7 * parea and region_text_chars(page, r) > 140:
            continue  # never rasterize a whole text page
        out.append(r)
    return out


# ---------- ordered page items (text + images), chrome/labels removed ----------
def page_items(pno):
    page = doc[pno]
    regions = image_regions(page)
    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        blk = Blk(b)
        if blk.lines and not is_chrome(blk):
            blocks.append(blk)

    # attach captions to regions; mark caption + inside-region blocks as consumed
    consumed = set()
    region_caps = {}
    for ri, r in enumerate(regions):
        best = None
        for bi, blk in enumerate(blocks):
            if bi in consumed:
                continue
            if CAP_RE.match(blk.text):
                below = blk.y0 - r.y1
                above = r.y0 - blk.y1
                gap = below if below >= -4 else (above if above >= -4 else None)
                if (
                    gap is not None
                    and gap < 60
                    and abs((blk.x0 + blk.x1) / 2 - (r.x0 + r.x1) / 2) < 0.5 * PAGE_W
                ):
                    if best is None or gap < best[0]:
                        best = (gap, bi, blk.text)
        if best:
            region_caps[ri] = re.sub(r"\s+", " ", best[2]).strip()
            consumed.add(best[1])
    for bi, blk in enumerate(blocks):
        c = blk.rect.tl + (blk.rect.br - blk.rect.tl) * 0.5
        for r in regions:
            if r.contains(c):
                consumed.add(bi)
                break

    items = []
    for bi, blk in enumerate(blocks):
        if bi in consumed:
            continue
        items.append(("text", blk.y0, blk.x0, blk))
    for ri, r in enumerate(regions):
        items.append(("image", r.y0, r.x0, (r, region_caps.get(ri))))

    # column detection: two columns only when a clear central gutter exists
    mid = PAGE_W / 2
    straddle = sum(
        1
        for k, y, x, p in items
        if k == "text" and p.x0 < mid - 0.05 * PAGE_W and p.x1 > mid + 0.05 * PAGE_W
    )
    left = [
        it
        for it in items
        if (it[3].x1 if it[0] == "text" else it[3][0].x1) <= mid + 0.02 * PAGE_W
    ]
    right = [it for it in items if it not in left]
    two_col = straddle <= 1 and len(left) >= 4 and len(right) >= 4

    if two_col:
        left.sort(key=lambda it: (round(it[1]), it[2]))
        right.sort(key=lambda it: (round(it[1]), it[2]))
        ordered = left + right
    else:
        ordered = sorted(items, key=lambda it: (round(it[1]), it[2]))
    return ordered


# ---------- inline formatting ----------
def fmt_spans(spans):
    parts = []
    for s in spans:
        t = s["text"]
        if t == "":
            continue
        f, sz, fl = s["font"], s["size"], s.get("flags", 0)
        e = html.escape(t)
        if is_mono(f):
            if is_bold(f, fl):
                e = f"<b>{e}</b>"
            e = f"<code>{e}</code>"
        elif is_italic(f, fl):
            e = f"<em>{e}</em>"
        elif is_bold(f, fl) and sz <= BODY_SIZE * 1.12:
            e = f"<strong>{e}</strong>"
        if fl & 1:
            e = f"<sup>{e}</sup>"
        parts.append(e)
    out = "".join(parts)
    for a in ("code", "em", "strong", "b"):
        out = out.replace(f"</{a}><{a}>", "")
    return re.sub(r"[ \t ]+", " ", out).strip()


def join_lines(html_lines):
    out = ""
    for hl in html_lines:
        if not out:
            out = hl
            continue
        m = re.search(r"([A-Za-z]{2,})-(</em>|</code>|</strong>|</b>)?$", out)
        if m and hl:
            inner = re.sub(r"^(<[^>]+>)+", "", hl)
            first = re.match(r"([A-Za-z]+)", inner)
            if first:
                w1, w2 = m.group(1), first.group(1)
                joined = (w1 + w2).lower()
                # Default: a line-break hyphen is soft, so join. Keep it only when
                # the compound is attested hyphenated elsewhere in the book, or the
                # second part is a proper noun / capitalized (e.g. "non-POSIX").
                keep = joined not in DICT and (
                    f"{w1.lower()}-{w2.lower()}" in KNOWN_HYPHENS or w2[:1].isupper()
                )
                if not keep:
                    out = out[: m.start()] + w1 + (m.group(2) or "") + hl
                    continue
            out += hl
            continue
        out += " " + hl
    return out


# ---------- code indentation ----------
def code_charwidth(blk):
    ws = []
    for ln in blk.lines:
        t = line_text(ln["spans"]).rstrip()
        n = len(t)
        if n >= 4:
            ws.append((ln["bbox"][2] - ln["bbox"][0]) / n)
    return statistics.median(ws) if ws else blk.lines[0]["spans"][0]["size"] * 0.5


# ---------- per-chapter conversion ----------
class Emitter:
    def __init__(self):
        self.el = []
        self.para = []
        self.para_kind = "p"
        self.code = []
        self.code_left = None
        self.code_lasty = None
        self.code_cw = None
        self.table = None
        self.headings = []

    def flush_para(self):
        if self.para:
            txt = join_lines(self.para)
            if txt:
                self.el.append((self.para_kind, txt))
        self.para, self.para_kind = [], "p"

    def flush_code(self):
        if self.code:
            self.el.append(("pre", "\n".join(self.code).strip("\n")))
        self.code, self.code_left, self.code_lasty, self.code_cw = [], None, None, None

    def flush_table(self):
        if self.table and self.table["rows"]:
            self.el.append(("table", self.table))
        self.table = None

    def flush_all(self):
        self.flush_para()
        self.flush_code()
        self.flush_table()


def looks_table_row(blk):
    """Multiple text clusters separated by wide horizontal gaps on each line."""
    multi = 0
    for ln in blk.lines:
        xs = sorted(
            (s["bbox"][0], s["bbox"][2]) for s in ln["spans"] if s["text"].strip()
        )
        if not xs:
            continue
        gaps = 0
        for a, b in zip(xs, xs[1:]):
            if b[0] - a[1] > 18:
                gaps += 1
        if gaps >= 1:
            multi += 1
    return multi >= max(1, len(blk.lines) // 2)


def table_cols(blk):
    xs = sorted(
        {
            round(s["bbox"][0])
            for ln in blk.lines
            for s in ln["spans"]
            if s["text"].strip()
        }
    )
    cols = []
    for x in xs:
        if not cols or x - cols[-1] > 14:
            cols.append(x)
    return cols


def assign_cells(blk, cols):
    cells = [[] for _ in cols]
    for ln in blk.lines:
        row = [[] for _ in cols]
        for s in ln["spans"]:
            if not s["text"].strip():
                continue
            ci = 0
            for j, cx in enumerate(cols):
                if s["bbox"][0] >= cx - 8:
                    ci = j
            row[ci].append(s)
        for j, sp in enumerate(row):
            if sp:
                cells[j].append(fmt_spans(sp))
    return [" ".join(c).strip() for c in cells]


def mono_frac(blk):
    mono = tot = 0
    for ln in blk.lines:
        for s in ln["spans"]:
            c = len(s["text"].strip())
            tot += c
            if is_mono(s["font"]):
                mono += c
    return (mono / tot) if tot else 0.0


def is_code_like(blk, txt):
    """A mono block is a code display (vs. a lone identifier) if it spans
    multiple lines, contains code punctuation, or is a multi-token line."""
    if len(blk.lines) >= 2:
        return True
    if re.search(r"[;{}()<>\[\]#=]", txt):
        return True
    s = txt.strip()
    if " " in s and len(s) > 10:
        return True
    return len(s) > 40


def convert_chapter(ci, ch):
    global fig_counter
    em = Emitter()
    heads = subentries[ci][:]

    items = []
    for pno in range(ch["start"], ch["end"]):
        for it in page_items(pno):
            items.append((pno,) + it)  # (pno, kind, y, x, payload)

    # x0 of the next text block after each item (for definition-term lookahead)
    n = len(items)
    next_text_x = [None] * n
    last = None
    for i in range(n - 1, -1, -1):
        next_text_x[i] = last
        if items[i][1] == "text":
            last = items[i][4].x0

    for i in range(n):
        pno, kind, y, x, payload = items[i]
        if kind == "image":
            r, cap = payload
            em.flush_all()
            fig_counter += 1
            name = f"fig{fig_counter}.png"
            doc[pno].get_pixmap(
                matrix=fitz.Matrix(2.4, 2.4), clip=r + (-4, -4, 4, 4)
            ).save(os.path.join(OEBPS, "images", name))
            em.el.append(("fig", (name, cap)))
            continue

        blk = payload
        font, size, flags = block_profile(blk)
        txt = re.sub(r"\s+", " ", blk.text).strip()
        if not txt:
            continue

        # skip the chapter-title banner at the very top of the opener page
        if (
            pno == ch["start"]
            and blk.y0 < PAGE_H * 0.18
            and norm(txt) == norm(ch["title"])
        ):
            continue

        # ---- heading: match against the TOC (font-independent), else bold+large ----
        depth = None
        tn = norm(txt)
        for k, (dep, t, p) in enumerate(heads):
            if norm(t) == tn and abs(p - pno) <= 1:
                depth = dep
                heads.pop(k)
                break
        if (
            depth is None
            and not is_mono(font)
            and len(txt) < 90
            and len(blk.lines) <= 2
            and is_bold(font, flags)
            and size >= BODY_SIZE * 1.15
            and not re.search(r"[.:;,]$", txt)
        ):
            depth = 2
        if depth is not None:
            em.flush_all()
            tag = "h%d" % min(4, 1 + depth)
            hid = f"h{ci}_{len(em.headings)}"
            em.headings.append((tag, txt, hid, depth))
            em.el.append((tag, (fmt_spans_plain(blk), hid)))
            continue

        # ---- table caption ----
        if re.match(r"^\s*table\s+\S+", txt, re.I) and len(txt) < 120:
            em.flush_para()
            em.flush_code()
            if not (txt.lower().endswith("(continued)") and em.table):
                em.flush_table()
                em.table = {"caption": txt, "cols": None, "header": None, "rows": []}
            continue

        # ---- table body rows ----
        if em.table is not None and looks_table_row(blk) and not is_mono(font):
            if em.table["cols"] is None:
                em.table["cols"] = table_cols(blk)
                em.table["header"] = assign_cells(blk, em.table["cols"])
            else:
                em.table["rows"].append(assign_cells(blk, em.table["cols"]))
            continue
        elif em.table is not None:
            em.flush_table()

        mfrac = mono_frac(blk)

        # ---- definition term: short label at the margin whose next block is indented ----
        nx = next_text_x[i]
        is_term = (
            blk.x0 <= BODY_LEFT + 8
            and len(blk.lines) <= 2
            and 0 < len(txt) < 70
            and not re.search(r"[.!?;,]$", txt)
            and (
                mfrac >= 0.5
                or is_bold(font, flags)
                or is_italic(font, flags)
                or len(txt) < 45
            )
        )
        if is_term and nx is not None and nx > BODY_LEFT + 16:
            em.flush_all()
            em.el.append(("dt", fmt_block(blk)))
            continue

        # ---- code block (mono-dominant AND actually code-shaped) ----
        if mfrac >= 0.6 and is_code_like(blk, txt):
            em.flush_para()
            if em.code_left is None:
                em.code_left = blk.x0
                em.code_cw = code_charwidth(blk)
            for ln in blk.lines:
                if (
                    em.code_lasty is not None
                    and ln["bbox"][1] - em.code_lasty > size * 1.7
                ):
                    em.code.append("")
                em.code_lasty = ln["bbox"][1]
                raw = line_text(ln["spans"])
                if not raw.startswith(" ") and em.code_cw:
                    ind = max(0, round((ln["bbox"][0] - em.code_left) / em.code_cw))
                    raw = " " * ind + raw
                em.code.append(html.escape(raw.rstrip()))
            continue
        else:
            em.flush_code()

        # ---- footnote / fine print ----
        if size <= BODY_SIZE * 0.82 and mfrac < 0.6:
            em.flush_para()
            em.el.append(("footnote", fmt_block(blk)))
            continue

        # ---- bullet list item ----
        if re.match(r"^\s*([•▪◦‣·⁃–—*])\s+", txt):
            em.flush_para()
            inner = re.sub(r"^\s*[•▪◦‣·⁃–—*]\s+", "", fmt_block(blk))
            em.el.append(("li", inner))
            continue

        # ---- paragraph (or indented description) ----
        lines_html = [h for h in (fmt_spans(ln["spans"]) for ln in blk.lines) if h]
        if not lines_html:
            continue
        kind_p = "dd" if blk.x0 > BODY_LEFT + 16 else "p"
        if em.para and em.para_kind == kind_p:
            prev = re.sub(r"<[^>]+>", "", em.para[-1]).strip()
            first = re.sub(r"<[^>]+>", "", lines_html[0]).strip()
            cont = (
                prev
                and not re.search(r'[.!?:"”)]$', prev)
                and first
                and (first[:1].islower() or prev.endswith((",", ";", "-")))
            )
            if not cont:
                em.flush_para()
        else:
            em.flush_para()
        em.para_kind = kind_p
        em.para.extend(lines_html)

    em.flush_all()
    return em


def fmt_spans_plain(blk):
    return re.sub(
        r"\s+", " ", " ".join(line_text(ln["spans"]) for ln in blk.lines)
    ).strip()


def fmt_block(blk):
    return join_lines(
        [fmt_spans(ln["spans"]) for ln in blk.lines if fmt_spans(ln["spans"])]
    )


# ---------- HTML assembly ----------
def esc(t):
    return html.escape(t)


def esc_keep(t):
    return (
        t
        if re.search(r"</?(em|code|strong|b|sup)>", t)
        else html.escape(t, quote=False)
    )


XHTML_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
"""


def elements_to_html(ch, em):
    out = [f'<h1 id="{ch["id"]}">{esc(ch["disp"])}</h1>']
    li_open = False

    def close_li():
        nonlocal li_open
        if li_open:
            out.append("</ul>")
            li_open = False

    for kind, payload in em.el:
        if kind != "li":
            close_li()
        if kind in ("h2", "h3", "h4"):
            txt, hid = payload
            out.append(f'<{kind} id="{hid}">{esc_keep(txt)}</{kind}>')
        elif kind == "p":
            out.append(f"<p>{payload}</p>")
        elif kind == "dt":
            out.append(f'<p class="dt">{payload}</p>')
        elif kind == "dd":
            out.append(f'<p class="dd">{payload}</p>')
        elif kind == "pre":
            out.append(f"<pre>{payload}</pre>")
        elif kind == "footnote":
            out.append(f'<p class="fn">{payload}</p>')
        elif kind == "li":
            if not li_open:
                out.append("<ul>")
                li_open = True
            out.append(f"<li>{payload}</li>")
        elif kind == "fig":
            img, cap = payload
            fc = f"<figcaption>{esc_keep(cap)}</figcaption>" if cap else ""
            alt = esc(cap) if cap else "Figure"
            out.append(f'<figure><img src="images/{img}" alt="{alt}"/>{fc}</figure>')
        elif kind == "table":
            tb = payload
            rows = [
                f'<p class="tcap">{esc_keep(tb["caption"])}</p>',
                '<div class="tw"><table>',
            ]
            if tb["header"]:
                rows.append(
                    "<thead><tr>"
                    + "".join(f"<th>{c}</th>" for c in tb["header"])
                    + "</tr></thead>"
                )
            rows.append("<tbody>")
            for r in tb["rows"]:
                rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
            rows.append("</tbody></table></div>")
            out.append("".join(rows))
    close_li()
    return "\n".join(out)


def write_chapter_file(ch, body):
    path = os.path.join(OEBPS, ch["id"] + ".xhtml")
    with open(path, "w") as f:
        f.write(XHTML_HEAD.format(title=esc(ch["disp"])))
        f.write(body)
        f.write("\n</body>\n</html>\n")
    ET.parse(path)


def write_css():
    css = """
html, body { margin: 0; padding: 0; }
body { text-align: justify; -webkit-hyphens: auto; hyphens: auto;
       line-height: 1.5; widows: 2; orphans: 2; }
h1, h2, h3, h4, .tcap, th
     { font-family: "Avenir Next", "Helvetica Neue", Helvetica, sans-serif;
       text-align: left; -webkit-hyphens: none; hyphens: none;
       page-break-after: avoid; break-after: avoid; line-height: 1.25; }
h1 { font-size: 1.7em; margin: 1.2em 0 .8em; }
h2 { font-size: 1.35em; margin: 1.6em 0 .5em; }
h3 { font-size: 1.15em; margin: 1.4em 0 .4em; }
h4 { font-size: 1em;    margin: 1.2em 0 .3em; }
p  { margin: 0 0 .65em; text-indent: 0; }
p.dt { margin: .5em 0 .1em; font-weight: 600; -webkit-hyphens: none; hyphens: none; }
p.dt code { font-weight: 600; }
p.dd { margin: 0 0 .5em 1.5em; }
p.fn { font-size: .82em; opacity: .85; margin: .5em 0; }
ul { margin: .4em 0 .8em 1.3em; padding: 0; }
li { margin-bottom: .3em; }
code { font-family: Menlo, "Courier New", monospace; font-size: .85em;
       -webkit-hyphens: none; hyphens: none; }
pre  { font-family: Menlo, "Courier New", monospace; font-size: .72em;
       line-height: 1.45; text-align: left; -webkit-hyphens: none; hyphens: none;
       white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word;
       background: rgba(128,128,128,.10); border: 1px solid rgba(128,128,128,.25);
       border-radius: 6px; padding: .8em 1em; margin: .8em 0; }
pre code { font-size: 1em; background: none; }
figure { margin: 1.2em 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
figcaption, .tcap { font-size: .85em; font-style: italic; margin: .5em 0 1em; }
figcaption { text-align: center; }
.tcap { text-align: left; margin: 1em 0 .3em; }
.tw { overflow-x: auto; margin: 0 0 1em; }
table { border-collapse: collapse; width: 100%; font-size: .88em; }
th, td { text-align: left; padding: .35em .6em; vertical-align: top;
         border-bottom: 1px solid rgba(128,128,128,.35); }
th { border-bottom: 2px solid rgba(128,128,128,.6); }
sup { font-size: .7em; line-height: 0; }
.cover { text-align: center; margin: 0; padding: 0; }
.cover img { max-width: 100%; max-height: 100%; }
"""
    with open(os.path.join(OEBPS, "style.css"), "w") as f:
        f.write(css)


def write_nav(items):
    li = []
    for disp, href, kids in items:
        sub = "".join(f'<li><a href="{k[2]}">{esc(k[1])}</a></li>' for k in kids)
        li.append(
            f'<li><a href="{href}">{esc(disp)}</a><ol>{sub}</ol></li>'
            if sub
            else f'<li><a href="{href}">{esc(disp)}</a></li>'
        )
    with open(os.path.join(OEBPS, "nav.xhtml"), "w") as f:
        f.write(XHTML_HEAD.format(title="Table of Contents"))
        f.write('<nav epub:type="toc" id="toc"><h1>Table of Contents</h1><ol>\n')
        f.write("\n".join(li))
        f.write(
            '\n</ol></nav>\n<nav epub:type="landmarks" hidden="hidden"><ol>'
            '<li><a epub:type="cover" href="cover.xhtml">Cover</a></li>'
            f'<li><a epub:type="bodymatter" href="{items[0][1]}">Begin Reading</a></li>'
            "</ol></nav>\n</body>\n</html>\n"
        )
    ET.parse(os.path.join(OEBPS, "nav.xhtml"))


def write_ncx(items):
    pts, n = [], 0
    for disp, href, kids in items:
        n += 1
        sub = ""
        for tag, txt, khref in kids:
            n += 1
            sub += (
                f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{esc(txt)}</text></navLabel>'
                f'<content src="{khref}"/></navPoint>'
            )
        pts.append(
            f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{esc(disp)}</text></navLabel>'
            f'<content src="{href}"/>{sub}</navPoint>'
        )
    with open(os.path.join(OEBPS, "toc.ncx"), "w") as f:
        f.write(f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{UID}"/></head>
<docTitle><text>{esc(META_TITLE)}</text></docTitle>
<navMap>{"".join(pts)}</navMap>
</ncx>
""")
    ET.parse(os.path.join(OEBPS, "toc.ncx"))


def write_opf():
    items = [
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="coverimg" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
        '<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="coverpage" linear="yes"/>']
    for ch in chapters:
        items.append(
            f'<item id="{ch["id"]}" href="{ch["id"]}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{ch["id"]}"/>')
    for fn in sorted(os.listdir(os.path.join(OEBPS, "images"))):
        if fn.startswith("fig"):
            items.append(
                f'<item id="{fn.split(".")[0]}" href="images/{fn}" media-type="image/png"/>'
            )
    with open(os.path.join(OEBPS, "content.opf"), "w") as f:
        f.write(f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="en">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">{UID}</dc:identifier>
<dc:title>{esc(META_TITLE)}</dc:title>
<dc:creator>{esc(META_AUTHOR)}</dc:creator>
<dc:language>en</dc:language>
<meta property="dcterms:modified">{MODIFIED}</meta>
<meta name="cover" content="coverimg"/>
</metadata>
<manifest>
{chr(10).join(items)}
</manifest>
<spine toc="ncx">
{chr(10).join(spine)}
</spine>
</package>
""")
    ET.parse(os.path.join(OEBPS, "content.opf"))


# ---------- metadata cleanup ----------
def clean_meta_title(raw, stem):
    t = (raw or "").strip()
    t = re.sub(r"^\s*microsoft\s+word\s*-\s*", "", t, flags=re.I)
    t = re.sub(r"\.(docx?|pdf|rtf|pages|indd)\s*$", "", t, flags=re.I).strip()
    if len(t) < 3 or t.lower() in ("untitled", "document", "book"):
        return stem
    return t


# ---------- driver ----------
def run(
    pdf_path,
    out_path,
    title=None,
    author=None,
    work_dir=None,
    modified=None,
    progress=None,
):
    global doc, BUILD, OEBPS, fig_counter, META_TITLE, META_AUTHOR, UID, MODIFIED
    fig_counter = 0

    def emit(m):
        if progress:
            progress(m)

    load_dict()
    doc = fitz.open(pdf_path)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    META_TITLE = title or clean_meta_title(doc.metadata.get("title"), stem)
    META_AUTHOR = author or (doc.metadata.get("author") or "").strip() or "Unknown"
    h = hashlib.sha1(META_TITLE.encode("utf-8")).hexdigest()
    UID = f"urn:uuid:{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    MODIFIED = modified or "2020-01-01T00:00:00Z"

    tmp = work_dir or tempfile.mkdtemp(prefix="pdf2epub_")
    BUILD = os.path.join(tmp, "build")
    OEBPS = os.path.join(BUILD, "OEBPS")
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(os.path.join(OEBPS, "images"))
    os.makedirs(os.path.join(BUILD, "META-INF"))

    emit(f"Opened '{META_TITLE}' ({doc.page_count} pages)")
    profile_document()
    emit(f"Body text ~{BODY_SIZE}pt; stripped {len(CHROME)} header/footer patterns")
    build_chapters()
    emit(f"Detected {len(chapters)} sections")

    doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).save(
        os.path.join(OEBPS, "images", "cover.png")
    )

    nav_items, stats = [], collections.Counter()
    for ci, ch in enumerate(chapters):
        em = convert_chapter(ci, ch)
        write_chapter_file(ch, elements_to_html(ch, em))
        for k, _ in em.el:
            stats[k] += 1
        kids = [
            (tag, txt, f'{ch["id"]}.xhtml#{hid}')
            for tag, txt, hid, dep in em.headings
            if tag in ("h2", "h3")
        ]
        nav_items.append((ch["disp"], ch["id"] + ".xhtml", kids))
        emit(f"Converted {ch['disp']}")

    with open(os.path.join(OEBPS, "cover.xhtml"), "w") as f:
        f.write(XHTML_HEAD.format(title="Cover"))
        f.write(
            f'<div class="cover"><img src="images/cover.png" alt="{esc(META_TITLE)}"/></div>'
        )
        f.write("\n</body>\n</html>\n")

    write_css()
    write_nav(nav_items)
    write_ncx(nav_items)
    write_opf()
    with open(os.path.join(BUILD, "META-INF", "container.xml"), "w") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
""")
    with open(os.path.join(BUILD, "mimetype"), "w") as f:
        f.write("application/epub+zip")

    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.write(
            os.path.join(BUILD, "mimetype"),
            "mimetype",
            compress_type=zipfile.ZIP_STORED,
        )
        for root, _, files in os.walk(BUILD):
            for fn in sorted(files):
                rel = os.path.relpath(os.path.join(root, fn), BUILD)
                if rel != "mimetype":
                    zf.write(
                        os.path.join(root, fn), rel, compress_type=zipfile.ZIP_DEFLATED
                    )

    doc.close()
    if work_dir is None:
        shutil.rmtree(tmp, ignore_errors=True)
    emit("Done")
    return {
        "title": META_TITLE,
        "chapters": len(chapters),
        "figures": fig_counter,
        "elements": dict(stats),
        "bytes": os.path.getsize(out_path),
    }


def main():
    ap = argparse.ArgumentParser(description="Convert a text PDF to reflowable EPUB 3.")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--title")
    ap.add_argument("--author")
    args = ap.parse_args()
    st = run(
        args.input,
        args.output,
        title=args.title,
        author=args.author,
        progress=lambda m: print("[*]", m),
    )
    print(st)


if __name__ == "__main__":
    main()
