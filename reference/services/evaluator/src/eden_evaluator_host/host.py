"""Evaluator worker host main loop."""

from __future__ import annotations

from pathlib import Path

from eden_contracts import EvaluationSchema
from eden_dispatch import ScriptedEvaluator
from eden_service_common import StopFlag, get_logger, make_evaluate_fn
from eden_storage import Store

log = get_logger(__name__)


def run_evaluator_loop(
    *,
    store: Store,
    worker_id: str,
    evaluation_schema: EvaluationSchema,
    fail_every: int | None,
    poll_interval: float,
    stop: StopFlag,
    artifacts_dir: Path | None = None,
) -> None:
    """Poll for pending evaluation tasks and drive each through the scripted profile.

    Returns only when ``stop`` is set.

    ``artifacts_dir`` (when set) opts into fixture-artifact emission:
    each evaluation writes a placeholder file under
    ``evaluations/<variant_id>/evaluation.txt`` and stamps a real
    ``file:///var/lib/eden/artifacts/...`` URI onto the
    EvaluationSubmission. ``None`` (the default) preserves the
    historical fictional ``file:///tmp/artifacts/...`` pointer.
    """
    if artifacts_dir is not None:
        Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
    evaluator = ScriptedEvaluator(
        worker_id=worker_id,
        evaluation_fn=make_evaluate_fn(
            evaluation_schema=evaluation_schema,
            fail_every=fail_every,
            artifacts_dir=artifacts_dir,
        ),
    )
    while not stop.is_set():
        processed = evaluator.run_pending(store, stop=stop.is_set)
        if processed == 0 and stop.wait(poll_interval):
            return
