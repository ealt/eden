"""Per-scenario-dir conftest — shared fixtures for the checkpoint scenarios.

The wave-5 checkpoint scenarios need both a SENDER IUT (which produces
the archive) and a RECEIVER IUT (which consumes it). The default
``wire_client`` fixture in ``conformance/conftest.py`` spawns exactly
one IUT per test and is hooked to an autouse fixture that
pre-registers worker ids — that makes the IUT non-empty, which
collides with the chapter-10 §11 "import requires a fresh store" rule
on the receiver side.

The fixtures below side-step that:

- :func:`receiver_iut` spawns an additional IUT per test, with a
  distinct ``experiment_id``, using the same adapter the suite is
  configured with (so non-reference adapters work transparently).
  The receiver is NOT seeded with default workers, satisfying the
  "store-must-be-empty" import precondition.
- :func:`receiver_wire_client` is a :class:`WireClient` bound to the
  receiver's base URL.
- :func:`sender_wire_client` is an alias for the default
  :func:`wire_client` so scenario code can name both sides
  symmetrically without ambiguity.

Cross-impl interop: the v1+checkpoints conformance index entry
documents the test as "only runs when two IUT adapters are
configured". v0 conformance is single-adapter; the cross-impl test
skips itself when ``--cross-impl-adapter`` is unset and the chapter-9
§5 group still has citation coverage via the other groups' tests.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from conformance.harness.adapter import IutAdapter, IutHandle
from conformance.harness.wire_client import WireClient


@pytest.fixture
def receiver_iut(
    iut_adapter_factory: type[IutAdapter],
    experiment_config_path: Path,
    tmp_path: Path,
) -> Iterator[IutHandle]:
    """Spawn a second IUT (the receiver) with a fresh experiment id.

    Uses ``iut_adapter_factory`` so non-reference adapters work
    transparently. The receiver is intentionally NOT seeded with
    default workers — chapter 10 §11 requires a fresh store for
    import to commit.
    """
    receiver_id = f"recv-{uuid.uuid4().hex[:8]}"
    adapter = iut_adapter_factory()
    cfg_copy = tmp_path / f"{receiver_id}-config.yaml"
    shutil.copyfile(experiment_config_path, cfg_copy)
    handle = adapter.start(
        experiment_config_path=cfg_copy,
        experiment_id=receiver_id,
    )
    try:
        yield handle
    finally:
        adapter.stop()


@pytest.fixture
def receiver_wire_client(
    receiver_iut: IutHandle,
    session_observed_problem_types: set[str],
) -> Iterator[WireClient]:
    """``WireClient`` bound to the receiver IUT."""
    with WireClient(
        base_url=receiver_iut.base_url,
        experiment_id=receiver_iut.experiment_id,
        extra_headers=receiver_iut.extra_headers,
        observed_problem_types=session_observed_problem_types,
    ) as client:
        yield client


@pytest.fixture
def sender_wire_client(wire_client: WireClient) -> WireClient:
    """Symmetric alias for the default ``wire_client`` (the sender)."""
    return wire_client


# ---------------------------------------------------------------------
# Wave 6 (v1+multi-experiment): control-plane subprocess fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def control_plane_handle() -> Iterator:  # type: ignore[type-arg]
    """Spawn the EDEN control-plane-server reference subprocess.

    The v1+multi-experiment scenarios drive the chapter 11 surface
    directly; they don't require the task-store-server, so the
    fixture is independent of `iut` / `wire_client`.

    Single-tenant per scenario: each test gets a fresh `:memory:`
    backing store + fresh port. Skipped when the suite is run
    against a non-reference adapter (the IUT contract is the
    chapter-7 binding; a non-reference IUT that exposes /v0/control/
    can pass v1+multi-experiment by configuring its own
    base URL via `--control-plane-base-url` in a future amendment).
    """
    from conformance.adapters.reference.control_plane_adapter import (
        ControlPlaneSubprocess,
    )

    cp = ControlPlaneSubprocess()
    handle = cp.start()
    try:
        yield handle
    finally:
        cp.stop()


@pytest.fixture
def control_plane_client(
    control_plane_handle,  # noqa: ANN001 — ControlPlaneHandle from helper
) -> Iterator:  # type: ignore[type-arg]
    """A `ControlPlaneClient` bound to the spawned subprocess."""
    from eden_control_plane import ControlPlaneClient

    client = ControlPlaneClient(control_plane_handle.base_url)
    try:
        yield client
    finally:
        client.close()
