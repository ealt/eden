"""HTTP wire binding for the EDEN protocol (spec/v0 chapter 7).

Exports:

- ``make_app(store)``: FastAPI application factory wrapping a ``Store``.
- ``StoreClient``: httpx-backed client that satisfies the ``Store``
  Protocol.
- ``WireError`` and the ``IndeterminateIntegration`` error used when
  an ``integrate_trial`` call's outcome cannot be determined after
  a transport-indeterminate failure.
"""

from __future__ import annotations

from .client import IndeterminateIntegration, StoreClient
from .errors import WireError
from .server import make_app

__all__ = [
    "IndeterminateIntegration",
    "StoreClient",
    "WireError",
    "make_app",
]
