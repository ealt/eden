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
def control_plane_base_url(iut: IutHandle) -> str:
    """The IUT's chapter 07 §15 control-plane base URL.

    Sourced from the active `iut` fixture's
    `IutHandle.control_plane_base_url`. Scenarios that bind to this
    fixture exercise the IUT under test (per chapter 9 §6's
    IUT-contract restriction), NOT a suite-managed subprocess.
    IUTs that don't expose the chapter 11 surface leave the field
    `None`, and the v1+multi-experiment scenarios skip via this
    fixture's `pytest.skip` branch — the chapter 11 level is
    parallel to v1+roles+integrator and v1+checkpoints, and IUTs
    MAY opt out per chapter 9 §4.
    """
    if iut.control_plane_base_url is None:
        pytest.skip(
            "IUT does not expose the chapter 07 §15 control-plane surface; "
            "skipping v1+multi-experiment scenarios"
        )
    return iut.control_plane_base_url


@pytest.fixture
def control_plane_client(
    iut: IutHandle,
    control_plane_base_url: str,
    session_observed_problem_types: set[str],
) -> Iterator:  # type: ignore[type-arg]
    """A `ControlPlaneWireClient` bound to the IUT's control-plane URL.

    Thin httpx wrapper from `conformance.harness.control_plane_client`
    that returns raw `httpx.Response` objects — scenarios assert on
    status codes + problem+json `type` strings so the suite stays
    IUT-agnostic per chapter 9 §6 (no Python exception classes from
    reference packages).

    Propagates `IutHandle.extra_headers` so auth-enabled non-reference
    IUTs that surface a session bearer / custom auth header through
    that field can drive the chapter-11 surface under the same auth
    posture as the chapter-2/4/5/7/8 scenarios.
    """
    from conformance.harness.control_plane_client import ControlPlaneWireClient

    client = ControlPlaneWireClient(
        base_url=control_plane_base_url,
        extra_headers=iut.extra_headers,
        observed_problem_types=session_observed_problem_types,
    )
    try:
        yield client
    finally:
        client.close()
