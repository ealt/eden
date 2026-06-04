"""Auto-checkpoint CLI wiring (issue #131).

Bridges the orchestrator CLI to the pure-logic
:class:`~eden_orchestrator.checkpoint_scheduler.CheckpointScheduler`:
startup validation (fail-fast on misconfiguration) and construction of
the scheduler plus its admin-bearer export client. Kept out of
``checkpoint_scheduler.py`` so that module stays free of CLI / wire
dependencies, and out of ``cli.py`` to keep the entrypoint focused.
"""

from __future__ import annotations

import os
from pathlib import Path

from eden_contracts import ExperimentConfig
from eden_wire import StoreClient

from .checkpoint_scheduler import CheckpointScheduler


def auto_checkpoint_enabled(config: ExperimentConfig) -> bool:
    """Whether the experiment-config opts into auto-checkpointing.

    Reference default is ``enabled=false`` (an absent block or absent
    ``enabled`` field is disabled), so this is true only when the block
    explicitly sets ``enabled: true``.
    """
    block = config.auto_checkpoint
    return bool(block is not None and block.enabled)


def validate_auto_checkpoint(
    *,
    args,  # noqa: ANN001 — argparse Namespace
    config: ExperimentConfig,
    admin_token: str | None,
) -> Path | None:
    """Fail fast on auto-checkpoint misconfiguration; resolve the dest dir.

    Returns the resolved, writable destination ``Path`` when
    auto-checkpointing is enabled, or ``None`` when it is disabled (the
    common case — the loop then holds a no-op scheduler). Raises
    :class:`SystemExit` with a clear operator-facing message when
    enabled but (a) no admin token is available for the admin-gated
    export endpoint (plan D4), or (b) the destination directory is
    unset, uncreatable, not a directory, or not writable (plan §3.3).
    """
    if not auto_checkpoint_enabled(config):
        return None
    if admin_token is None:
        raise SystemExit(
            "auto_checkpoint.enabled is true but no admin token is "
            "available; set --admin-token / $EDEN_ADMIN_TOKEN so the "
            "orchestrator can call the admin-gated checkpoint export "
            "endpoint (07-wire-protocol.md §14)."
        )
    if not args.auto_checkpoint_dir:
        raise SystemExit(
            "auto_checkpoint.enabled is true but no destination directory "
            "is set; pass --auto-checkpoint-dir or set "
            "$EDEN_AUTO_CHECKPOINT_DIR (the host path is deployment "
            "wiring, not part of the portable config block)."
        )
    path = Path(args.auto_checkpoint_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"--auto-checkpoint-dir {str(path)!r}: cannot create "
            f"directory: {exc}"
        ) from exc
    if not path.is_dir():
        raise SystemExit(
            f"--auto-checkpoint-dir {str(path)!r}: exists but is not a "
            "directory."
        )
    if not os.access(path, os.W_OK):
        raise SystemExit(f"--auto-checkpoint-dir {str(path)!r}: not writable.")
    return path


def build_auto_checkpoint_scheduler(
    *,
    args,  # noqa: ANN001 — argparse Namespace
    config: ExperimentConfig,
    admin_token: str | None,
    destination: Path | None,
    log,  # noqa: ANN001 — _CtxAdapter
) -> tuple[CheckpointScheduler, StoreClient | None]:
    """Construct the auto-checkpoint scheduler + its admin export client.

    When ``destination`` is ``None`` (auto-checkpointing disabled) the
    scheduler is a no-op and there is no export client. When enabled,
    a SECOND, narrowly-scoped :class:`StoreClient` authed with the
    deployment admin bearer (``admin:<token>``) is built — the checkpoint
    export endpoint is admin-gated per ``07-wire-protocol.md`` §14, so
    the loop's worker-bearer client would 403 (plan D4). The returned
    client is owned by the caller, which closes it after the loop.
    """
    if destination is None:
        return (
            CheckpointScheduler.from_config(
                config.auto_checkpoint,
                experiment_id=args.experiment_id,
                destination=None,
                export_fn=None,
            ),
            None,
        )
    # validate_auto_checkpoint guarantees admin_token is set when enabled.
    assert admin_token is not None  # noqa: S101 — invariant from validation
    export_client = StoreClient(
        args.task_store_url,
        args.experiment_id,
        bearer=f"admin:{admin_token}",
    )

    def _export(stream) -> None:  # noqa: ANN001 — binary IO stream
        export_client.export_checkpoint(stream)

    scheduler = CheckpointScheduler.from_config(
        config.auto_checkpoint,
        experiment_id=args.experiment_id,
        destination=destination,
        export_fn=_export,
    )
    log.info(
        "auto_checkpoint_enabled",
        destination=str(destination),
        interval_seconds=scheduler.interval_seconds,
        retention_count=scheduler.retention_count,
        on_terminate=scheduler.on_terminate,
    )
    return scheduler, export_client
