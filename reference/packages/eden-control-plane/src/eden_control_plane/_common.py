"""Shared type aliases used across eden-control-plane models.

Mirrors `reference/packages/eden-contracts/src/eden_contracts/_common.py`
for the validators that the eden-contracts public surface does NOT
re-export (specifically, the URI validator). When eden-contracts
widens its public surface, this module SHOULD shrink to a re-export.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator
from rfc3986_validator import validate_rfc3986


def _check_uri(value: str) -> str:
    if not validate_rfc3986(value, rule="URI"):
        raise ValueError(f"not a valid RFC 3986 URI: {value!r}")
    return value


ConfigUriStr = Annotated[str, AfterValidator(_check_uri)]
"""RFC 3986 URI for the experiment-config resource (chapter 11 §2.1)."""
