import os
import sys

# The engine lives under docs/engine so it can be served statically to Pyodide;
# make that package importable for tests and the CLI under normal CPython.
ENGINE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "engine"
)
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)
