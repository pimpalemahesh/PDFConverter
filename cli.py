#!/usr/bin/env python3
"""Local command-line entry point for the pdf2epub engine (CPython).

    python cli.py input.pdf output.epub --title "My Book" --author "Someone"

The browser app uses the very same engine module (docs/engine/pdf2epub).
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "engine")
)

from pdf2epub.converter import main  # noqa: E402

if __name__ == "__main__":
    main()
