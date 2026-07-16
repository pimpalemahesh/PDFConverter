"""pdf2epub — reflowable EPUB 3 builder for text-based PDFs.

The engine is deliberately dependency-light: PyMuPDF (fitz) plus the Python
standard library only, so the exact same module runs under CPython (tests, CLI)
and under Pyodide (the browser app).
"""

from .converter import run

__all__ = ["run"]
__version__ = "2.0.0"
