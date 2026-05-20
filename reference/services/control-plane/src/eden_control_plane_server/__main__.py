"""Entry point for `python -m eden_control_plane_server`."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
