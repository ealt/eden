"""pytest plugin for the EDEN conformance suite.

Wires fixtures (iut, wire_client, event_log, experiment_id),
markers (conformance, conformance_meta), and the --iut-adapter CLI
option that selects which adapter implementation to drive.
"""

from __future__ import annotations

import importlib
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from .adapter import IutAdapter, IutHandle
from .event_cursor import EventLog
from .wire_client import WireClient

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_DEFAULT_EXPERIMENT_CONFIG = _FIXTURES_DIR / "minimal-experiment.yaml"
_DEFAULT_ADAPTER = "conformance.adapters.reference.adapter:ReferenceAdapter"

OBSERVED_PROBLEM_TYPES_KEY = pytest.StashKey[set[str]]()


def pytest_configure(config: pytest.Config) -> None:
    """Register markers and observation-state stashes."""
    config.addinivalue_line(
        "markers",
        "conformance: scenario asserts a chapter-2/4/5/7/8 task-store-or-wire MUST. "
        "Every IUT claiming v1 conformance must pass.",
    )
    config.addinivalue_line(
        "markers",
        "conformance_meta: harness self-validation. Not part of any conformance level.",
    )
    config.stash[OBSERVED_PROBLEM_TYPES_KEY] = set()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add --iut-adapter CLI flag."""
    parser.addoption(
        "--iut-adapter",
        default=_DEFAULT_ADAPTER,
        help="Importable path 'module:Class' to the IutAdapter implementation.",
    )
    parser.addoption(
        "--experiment-config",
        default=str(_DEFAULT_EXPERIMENT_CONFIG),
        help="Path to the experiment-config YAML the IUT will be started against.",
    )


@pytest.fixture(scope="session")
def iut_adapter_factory(pytestconfig: pytest.Config) -> type[IutAdapter]:
    """Resolve --iut-adapter to a callable adapter class."""
    spec_value = pytestconfig.getoption("--iut-adapter")
    if not isinstance(spec_value, str):
        raise pytest.UsageError("--iut-adapter must be a 'module:Class' string")
    module_name, cls_name = spec_value.split(":")
    cls = getattr(importlib.import_module(module_name), cls_name)
    return cls


@pytest.fixture(scope="session")
def experiment_config_path(pytestconfig: pytest.Config) -> Path:
    raw = pytestconfig.getoption("--experiment-config")
    if not isinstance(raw, str):
        raise pytest.UsageError("--experiment-config must be a path")
    return Path(raw).resolve()


@pytest.fixture(scope="session")
def session_observed_problem_types(pytestconfig: pytest.Config) -> set[str]:
    """Session-scoped accumulator of every `eden://error/...` `type` seen.

    Populated automatically by ``WireClient`` on every problem+json
    response. The vocabulary-closure scenario reads this set to assert
    chapter 7 §7 closure both ways.
    """
    return pytestconfig.stash[OBSERVED_PROBLEM_TYPES_KEY]


@pytest.fixture
def experiment_id() -> str:
    """Fresh experiment id per scenario to isolate state."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def iut(
    iut_adapter_factory: type[IutAdapter],
    experiment_config_path: Path,
    experiment_id: str,
    tmp_path: Path,
) -> Iterator[IutHandle]:
    """Per-scenario IUT lifecycle."""
    adapter = iut_adapter_factory()
    cfg_copy = tmp_path / "experiment-config.yaml"
    shutil.copyfile(experiment_config_path, cfg_copy)
    handle = adapter.start(
        experiment_config_path=cfg_copy,
        experiment_id=experiment_id,
    )
    try:
        yield handle
    finally:
        adapter.stop()


@pytest.fixture
def wire_client(
    iut: IutHandle,
    session_observed_problem_types: set[str],
) -> Iterator[WireClient]:
    """httpx-backed WireClient bound to the started IUT."""
    with WireClient(
        base_url=iut.base_url,
        experiment_id=iut.experiment_id,
        extra_headers=iut.extra_headers,
        observed_problem_types=session_observed_problem_types,
    ) as client:
        yield client


@pytest.fixture
def event_log(wire_client: WireClient) -> EventLog:
    return EventLog(wire_client)
