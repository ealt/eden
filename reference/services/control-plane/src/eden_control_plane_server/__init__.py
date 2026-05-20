"""FastAPI server hosting the EDEN control plane.

See `app.py` for `make_app`; `cli.py` for the entry point.
"""

from .app import build_store, make_app

__all__ = ["build_store", "make_app"]
