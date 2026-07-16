"""Engine tests that run under CPython (the same module also runs in Pyodide).

A small PDF is synthesized with PyMuPDF so the suite is self-contained; if the
sample books are present locally, richer assertions run too.
"""

import os
import zipfile

import fitz
import pytest

import pdf2epub


def _make_pdf(path):
    doc = fitz.open()
    for n in range(1, 4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), f"Section {n}", fontsize=18, fontname="helv")
        page.insert_text(
            (72, 130),
            "This is body text that should reflow into a real "
            "paragraph inside the generated EPUB file.",
            fontsize=11,
        )
        page.insert_text(
            (72, 200), "int main(void) { return 0; }", fontsize=9, fontname="cour"
        )  # monospace -> code
    # a simple 2-level outline so chapter splitting kicks in
    doc.set_toc([[1, "Section 1", 1], [1, "Section 2", 2], [1, "Section 3", 3]])
    doc.save(path)
    doc.close()


@pytest.fixture
def sample_pdf(tmp_path):
    p = str(tmp_path / "sample.pdf")
    _make_pdf(p)
    return p


def test_produces_valid_epub(sample_pdf, tmp_path):
    out = str(tmp_path / "out.epub")
    stats = pdf2epub.run(sample_pdf, out, title="Test Book", author="Tester")

    assert os.path.exists(out)
    assert stats["title"] == "Test Book"
    assert stats["chapters"] >= 3

    with zipfile.ZipFile(out) as z:
        info = z.infolist()
        # EPUB OCF rule: mimetype must be first and stored (uncompressed)
        assert info[0].filename == "mimetype"
        assert info[0].compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        assert z.testzip() is None
        names = z.namelist()
        assert "OEBPS/content.opf" in names
        assert "OEBPS/nav.xhtml" in names
        assert any(n.endswith(".xhtml") and "OEBPS/c" in n for n in names)


def test_code_is_preserved(sample_pdf, tmp_path):
    out = str(tmp_path / "out.epub")
    pdf2epub.run(sample_pdf, out)
    with zipfile.ZipFile(out) as z:
        body = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    assert "<pre>" in body and "int main" in body


def test_title_falls_back_to_filename(tmp_path):
    p = str(tmp_path / "My Great Book.pdf")
    _make_pdf(p)
    out = str(tmp_path / "o.epub")
    stats = pdf2epub.run(p, out)
    assert stats["title"] == "My Great Book"


BOOKS = "/Users/mahesh/Books/Linux System Programming.pdf"


@pytest.mark.skipif(not os.path.exists(BOOKS), reason="sample book not present")
def test_real_book_has_chapters_and_figures(tmp_path):
    out = str(tmp_path / "linux.epub")
    stats = pdf2epub.run(BOOKS, out)
    assert stats["chapters"] >= 10
    assert stats["figures"] >= 2
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
