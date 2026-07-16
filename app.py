#!/usr/bin/env python3
"""
PDF -> EPUB web server.

Upload a PDF, get back a clean, reflowable EPUB 3 tuned to render well in
Apple Books (justified text, de-hyphenated paragraphs, styled code blocks,
real tables, extracted figures, a navigable TOC, and dark-mode-safe styling).

Run:
    ./venv/bin/python app.py
    open http://127.0.0.1:5000
"""
import os
import re
import uuid
import datetime
import traceback

from flask import (Flask, request, render_template, send_from_directory,
                   redirect, url_for, flash, abort)
from werkzeug.utils import secure_filename

import converter

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
OUTPUTS = os.path.join(BASE, "outputs")
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(OUTPUTS, exist_ok=True)

MAX_MB = 100

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.secret_key = "pdf-converter-local"


def _slug(name):
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r"[^A-Za-z0-9._ -]", "", stem).strip() or "book"
    return stem


@app.route("/", methods=["GET"])
def index():
    files = []
    for fn in sorted(os.listdir(OUTPUTS)):
        if fn.endswith(".epub"):
            p = os.path.join(OUTPUTS, fn)
            files.append({"name": fn, "size_kb": round(os.path.getsize(p) / 1024)})
    return render_template("index.html", files=files, max_mb=MAX_MB)


@app.route("/convert", methods=["POST"])
def convert():
    f = request.files.get("pdf")
    if not f or f.filename == "":
        flash("Please choose a PDF file.", "error")
        return redirect(url_for("index"))
    if not f.filename.lower().endswith(".pdf"):
        flash("That doesn't look like a PDF.", "error")
        return redirect(url_for("index"))

    title = (request.form.get("title") or "").strip() or None
    author = (request.form.get("author") or "").strip() or None

    token = uuid.uuid4().hex[:8]
    safe = secure_filename(f.filename) or "upload.pdf"
    in_path = os.path.join(UPLOADS, f"{token}_{safe}")
    f.save(in_path)

    stem = _slug(title or f.filename)
    out_name = f"{stem}.epub"
    out_path = os.path.join(OUTPUTS, out_name)
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        stats = converter.run(in_path, out_path, title=title, author=author,
                              modified=modified)
    except Exception:
        traceback.print_exc()
        flash("Conversion failed — see the server log for details. This tool "
              "works best on text-based (not scanned) PDFs.", "error")
        return redirect(url_for("index"))
    finally:
        try:
            os.remove(in_path)
        except OSError:
            pass

    flash(f"Converted “{stats['title']}” — {stats['chapters']} sections, "
          f"{stats['figures']} figures, {round(stats['bytes']/1024)} KB.", "ok")
    return redirect(url_for("index"))


@app.route("/download/<path:name>")
def download(name):
    if not name.endswith(".epub") or "/" in name or ".." in name:
        abort(404)
    return send_from_directory(OUTPUTS, name, as_attachment=True,
                               mimetype="application/epub+zip")


@app.route("/delete/<path:name>", methods=["POST"])
def delete(name):
    if name.endswith(".epub") and "/" not in name and ".." not in name:
        try:
            os.remove(os.path.join(OUTPUTS, name))
            flash(f"Deleted {name}.", "ok")
        except OSError:
            pass
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
