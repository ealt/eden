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
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from _pytest.terminal import TerminalReporter

from .adapter import IutAdapter, IutHandle
from .error_vocabulary import out_of_vocabulary, unobserved_core
from .event_cursor import EventLog
from .wire_client import WireClient

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_DEFAULT_EXPERIMENT_CONFIG = _FIXTURES_DIR / "minimal-experiment.yaml"
_DEFAULT_ADAPTER = "conformance.adapters.reference.adapter:ReferenceAdapter"

OBSERVED_PROBLEM_TYPES_KEY = pytest.StashKey[set[str]]()
# Controller-side union of every worker's observed problem types, used
# to assert the chapter 07 §7 vocabulary closure across a distributed
# (pytest-xdist) run. See `pytest_testnodedown` / `pytest_sessionfinish`.
CONTROLLER_OBSERVED_UNION_KEY = pytest.StashKey[set[str]]()
# Set True on the controller if any worker did NOT finish cleanly (crash
# path, or `workeroutput` missing our key). When set, the cross-worker
# union is partial, so the controller skips the closure assertion rather
# than emit a misleading "core type never observed" failure on top of the
# already-red run.
CONTROLLER_AGGREGATE_INCOMPLETE_KEY = pytest.StashKey[bool]()

_WORKEROUTPUT_OBSERVED_KEY = "eden_observed_problem_types"


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
    config.stash[CONTROLLER_OBSERVED_UNION_KEY] = set()
    config.stash[CONTROLLER_AGGREGATE_INCOMPLETE_KEY] = False


def _is_xdist_worker(config: pytest.Config) -> bool:
    """True when this process is a pytest-xdist worker (not the controller)."""
    return hasattr(config, "workerinput")


def _is_distributed(config: pytest.Config) -> bool:
    """True when the run is distributed across workers (``-n>0``)."""
    # `dist` is "no" for a serial run and the scheduling mode (e.g.
    # "load") when xdist is driving workers.
    return str(config.getoption("dist", "no")) != "no"


def pytest_testnodedown(node: object, error: object) -> None:
    """Controller-side: fold a finished worker's observed types into the union.

    xdist calls this on the controller as each worker shuts down,
    before the controller's ``pytest_sessionfinish``. ``workeroutput``
    carries the worker's accumulated ``observed_problem_types`` (written
    in the worker's ``pytest_sessionfinish`` below).

    xdist invokes this on BOTH the clean shutdown path and the
    crash path (``worker_errordown``). On a crash ``error`` is set and
    ``workeroutput`` may be missing or partial, so the union would be
    incomplete — flag it so the controller skips the closure assertion
    rather than report a spurious "core type never observed" failure.
    """
    config = getattr(node, "config", None)
    if config is None:
        return
    workeroutput = getattr(node, "workeroutput", None) or {}
    if error is not None or _WORKEROUTPUT_OBSERVED_KEY not in workeroutput:
        config.stash[CONTROLLER_AGGREGATE_INCOMPLETE_KEY] = True
        # Still fold in whatever this worker did report — it can only
        # add real observations, never invent a missing one.
    config.stash[CONTROLLER_OBSERVED_UNION_KEY].update(
        workeroutput.get(_WORKEROUTPUT_OBSERVED_KEY, [])
    )


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Worker: publish observed types. Controller: assert the §7 closure.

    Under pytest-xdist the per-test vocabulary-closure scenarios skip
    (they would only see their own worker's partial accumulator), so
    the closure is asserted here on the controller over the cross-worker
    union. Serial runs assert in the scenarios themselves and this hook
    is a no-op.
    """
    config = session.config
    if _is_xdist_worker(config):
        # Hand this worker's observations to the controller.
        config.workeroutput[_WORKEROUTPUT_OBSERVED_KEY] = sorted(  # type: ignore[attr-defined]
            config.stash[OBSERVED_PROBLEM_TYPES_KEY]
        )
        return
    if not _is_distributed(config):
        # Serial run: test_error_vocabulary.py asserted the closure.
        return
    if config.stash[CONTROLLER_AGGREGATE_INCOMPLETE_KEY]:
        # A worker did not finish cleanly — the union is partial, so the
        # closure would be a misleading failure on top of the real one.
        # The worker crash already fails the run.
        reporter = cast(
            "TerminalReporter | None",
            config.pluginmanager.getplugin("terminalreporter"),
        )
        if reporter is not None:
            reporter.write_line(
                "error-vocabulary closure NOT asserted: a worker did not "
                "finish cleanly, so the cross-worker observation union is "
                "incomplete.",
                yellow=True,
            )
        return

    union = set(config.stash[CONTROLLER_OBSERVED_UNION_KEY])
    failures: list[str] = []
    extras = out_of_vocabulary(union)
    if extras:
        failures.append(
            "spec/v0/07-wire-protocol.md §9 — observed `type` URIs outside "
            f"the §7 closed vocabulary: {sorted(extras)}"
        )
    missing = unobserved_core(union)
    if missing:
        failures.append(
            "spec/v0/07-wire-protocol.md §9 — §7 core vocabulary entries "
            f"never observed during the run: {sorted(missing)}"
        )
    if failures:
        reporter = cast(
            "TerminalReporter | None",
            config.pluginmanager.getplugin("terminalreporter"),
        )
        if reporter is not None:
            reporter.write_sep("=", "error-vocabulary closure (xdist aggregate)", red=True)
            for line in failures:
                reporter.write_line(line, red=True)
        # Promote to a failing session exit code without clobbering an
        # already-failing status from real test failures.
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


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


@pytest.fixture(autouse=True)
def default_workers(wire_client: WireClient) -> None:
    """Pre-register the conventional worker_ids for every conformance scenario.

    12a-1 wave 5: ``Store.claim`` rejects unregistered worker_ids
    with ``WorkerNotRegistered`` per spec §3.5 step 2. Scenarios that
    drive a happy-path claim/submit cycle inherit this fixture so
    they don't have to re-register the conventional ids
    (``test-worker``, ``impl-worker``, ``eval-worker``, ``worker-a``,
    ``worker-b``). Scenarios that explicitly probe the
    unregistered-worker path register additional ids inline.
    """
    from ._seed import register_default_workers

    register_default_workers(wire_client)
