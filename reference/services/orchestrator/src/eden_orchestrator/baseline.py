"""Seed baseline-variant creation (``02-data-model.md`` §9.4).

The orchestrator elevates the experiment seed to a first-class
``kind == "baseline"`` variant so it can be scored and compared like any
other variant. This module owns the idempotent creation step invoked at
startup by both orchestrator modes (single-experiment ``cli.py`` and the
per-experiment branch of ``multi_loop.py``).

Idempotency is by **verified read-back**, not a blind ``AlreadyExists``
catch: an unrelated row at the deterministic id — or a seed/config drift
across restarts — must fail loudly rather than silently suppress the
baseline (``02-data-model.md`` §9.4, plan §D.2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from eden_contracts import ExperimentConfig, Variant
from eden_service_common import get_logger
from eden_storage import Store
from eden_storage.errors import AlreadyExists, NotFound, StorageError

_log = get_logger(__name__)

BASELINE_VARIANT_ID = "baseline"
"""Deterministic variant id for the seed baseline.

A fixed id (rather than a random one) makes concurrent orchestrator
instances / loop iterations converge on the same record and re-runs
idempotent. Single-baseline per experiment (§9.4), so one id suffices.
"""


def _now_iso() -> str:
    """Return an RFC 3339 UTC timestamp with millisecond precision + ``Z``.

    Matches the store's ``_ts`` shape (``02-data-model.md`` §1.2) so the
    value validates against ``DateTimeStr``.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _baseline_enabled(config: ExperimentConfig) -> bool:
    """An absent ``baseline`` block — or one without ``enabled`` — is default-on."""
    baseline = config.baseline
    return baseline is None or baseline.enabled is not False


def ensure_baseline_variant(
    *,
    store: Store,
    config: ExperimentConfig,
    experiment_id: str,
) -> None:
    """Create the seed baseline variant if enabled and not already present.

    Reads ``experiment.base_commit_sha`` (``02-data-model.md`` §2.5) — the
    per-experiment authoritative seed source for both orchestrator modes.
    When it is absent (a pre-field / legacy experiment) creation is
    **skipped with a warning**, never a hard failure: a crash here would
    take down the whole process in multi-experiment mode over one legacy
    experiment (plan §D.2).

    Idempotent by verified read-back: if the deterministic-id variant
    already exists it MUST be a ``kind == "baseline"`` variant whose
    ``commit_sha`` equals the seed, else this raises (seed/config drift is
    an operator error). When absent, the variant is created on the default
    path (``starting`` — the ``evaluation_dispatch`` decision then scores
    it) or, when ``baseline.metrics`` is supplied, the override path
    (directly ``success`` with the config metrics, skipping evaluation).
    """
    if not _baseline_enabled(config):
        return
    try:
        experiment = store.read_experiment()
    except StorageError:
        _log.warning("ensure_baseline_read_experiment_failed", experiment_id=experiment_id)
        return
    base_sha = experiment.base_commit_sha
    if base_sha is None:
        _log.warning(
            "baseline_enabled_but_no_base_commit_sha",
            experiment_id=experiment_id,
            detail="experiment has no base_commit_sha; skipping baseline creation",
        )
        return

    try:
        existing: Variant | None = store.read_variant(BASELINE_VARIANT_ID)
    except NotFound:
        existing = None
    except StorageError:
        _log.warning("ensure_baseline_read_variant_failed", experiment_id=experiment_id)
        return

    if existing is not None:
        if existing.kind != "baseline" or existing.commit_sha != base_sha:
            raise RuntimeError(
                f"variant {BASELINE_VARIANT_ID!r} exists but is not the expected "
                f"baseline for experiment {experiment_id!r}: kind={existing.kind!r}, "
                f"commit_sha={existing.commit_sha!r} (expected kind='baseline', "
                f"commit_sha={base_sha!r}). Seed/config drift — refusing to proceed."
            )
        return  # already created; idempotent no-op (no event re-emit)

    metrics = config.baseline.metrics if config.baseline is not None else None
    ts = _now_iso()
    if metrics is None:
        # Default path: created `starting`; evaluation_dispatch scores it.
        variant = Variant(
            variant_id=BASELINE_VARIANT_ID,
            experiment_id=experiment_id,
            kind="baseline",
            status="starting",
            parent_commits=[base_sha],
            commit_sha=base_sha,
            started_at=ts,
        )
        path = "default"
    else:
        # Override path: created directly `success` with config metrics.
        variant = Variant(
            variant_id=BASELINE_VARIANT_ID,
            experiment_id=experiment_id,
            kind="baseline",
            status="success",
            parent_commits=[base_sha],
            commit_sha=base_sha,
            evaluation=dict(metrics),
            started_at=ts,
            completed_at=ts,
        )
        path = "override"

    try:
        store.create_variant(variant)
    except AlreadyExists:
        # A concurrent instance won the race; the verified read-back on the
        # next iteration confirms it. Do NOT treat as an error and do NOT
        # re-emit events.
        _log.info("baseline_variant_already_created", experiment_id=experiment_id)
        return
    _log.info(
        "baseline_variant_created",
        experiment_id=experiment_id,
        path=path,
        commit_sha=base_sha,
    )
