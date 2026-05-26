"""IUT adapter Protocol for the EDEN conformance suite.

A conforming IUT (implementation under test) is anything that exposes
the chapter-7 HTTP wire binding. The adapter Protocol is informative
per spec/v0/09-conformance.md §6 — it is one convenience for IUTs
that want to integrate with this Python harness, not a normative
requirement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class IutHandle:
    """Handle returned by IutAdapter.start.

    Carries everything the harness needs to drive the IUT through
    chapter-7 HTTP. The handle is flat by design: every secret a
    scenario needs is observable through the wire binding, so the
    handle does not expose callbacks for fetching tokens mid-scenario.

    `control_plane_base_url` is OPTIONAL; populated by IUTs that
    expose the chapter 07 §15 surface for the v1+multi-experiment
    conformance level. The reference adapter sets it after
    spawning its own control-plane subprocess; non-reference IUTs
    that bundle the control plane behind the same base_url MAY
    surface it as the same value as `base_url`; IUTs that don't
    support v1+multi-experiment leave it `None` and the
    multi-experiment scenarios skip.

    `admin_token` is the §13.1 admin secret the harness uses to
    register workers / groups against an auth-enabled IUT.
    Adapters that surface auth via a different mechanism (e.g. a
    custom Authorization header in ``extra_headers``) MAY leave it
    ``None``; the harness then falls back to the bearer carried in
    ``extra_headers`` for admin-class operations.
    """

    base_url: str
    experiment_id: str
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    control_plane_base_url: str | None = None
    admin_token: str | None = None


@runtime_checkable
class IutAdapter(Protocol):
    """Lifecycle contract for a conformance-suite subject.

    The adapter starts a fresh subject before each scenario and tears
    it down after. Header-based auth (Authorization: Bearer, custom
    header, or none) is supported via IutHandle.extra_headers;
    transport-level auth (mTLS, IP allowlists) is not currently
    supported by this adapter shape.
    """

    def start(
        self,
        *,
        experiment_config_path: Path,
        experiment_id: str,
    ) -> IutHandle: ...

    def stop(self) -> None: ...
