"""HTTP wire binding for the EDEN protocol (spec/v0 chapter 7).

Exports:

- ``make_app(store)``: FastAPI application factory wrapping a ``Store``.
- ``StoreClient``: httpx-backed client that satisfies the ``Store``
  Protocol.
- ``WireError`` and the ``Indeterminate*`` errors used when the wire
  call's outcome cannot be determined after a transport-indeterminate
  failure (``IndeterminateIntegration`` for ``integrate_variant``;
  ``IndeterminateReassign`` for ``reassign_task``;
  ``IndeterminateDispatchModeUpdate`` for ``update_dispatch_mode``;
  ``IndeterminateTermination`` for ``terminate_experiment``).
- ``Principal`` / ``parse_bearer`` / ``authenticate`` from the §13
  auth module.
"""

from __future__ import annotations

from .auth import Principal, authenticate, parse_bearer
from .client import (
    IndeterminateDispatchModeUpdate,
    IndeterminateIntegration,
    IndeterminateReassign,
    IndeterminateTermination,
    StoreClient,
)
from .errors import Forbidden, Unauthorized, WireError, WireReferenceError
from .server import make_app

__all__ = [
    "Forbidden",
    "IndeterminateDispatchModeUpdate",
    "IndeterminateIntegration",
    "IndeterminateReassign",
    "IndeterminateTermination",
    "Principal",
    "StoreClient",
    "Unauthorized",
    "WireError",
    "WireReferenceError",
    "authenticate",
    "make_app",
    "parse_bearer",
]
