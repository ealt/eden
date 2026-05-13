"""HTTP wire binding for the EDEN protocol (spec/v0 chapter 7).

Exports:

- ``make_app(store)``: FastAPI application factory wrapping a ``Store``.
- ``StoreClient``: httpx-backed client that satisfies the ``Store``
  Protocol.
- ``WireError`` and the ``IndeterminateIntegration`` error used when
  an ``integrate_variant`` call's outcome cannot be determined after
  a transport-indeterminate failure.
- ``Principal`` / ``parse_bearer`` / ``authenticate`` from the §13
  auth module.
"""

from __future__ import annotations

from .auth import Principal, authenticate, parse_bearer
from .client import IndeterminateIntegration, StoreClient
from .errors import Forbidden, Unauthorized, WireError, WireReferenceError
from .server import make_app

__all__ = [
    "Forbidden",
    "IndeterminateIntegration",
    "Principal",
    "StoreClient",
    "Unauthorized",
    "WireError",
    "WireReferenceError",
    "authenticate",
    "make_app",
    "parse_bearer",
]
