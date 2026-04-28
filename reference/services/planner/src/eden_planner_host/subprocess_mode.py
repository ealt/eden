"""Phase 10d planner-host subprocess mode.

Runs a single long-running subprocess for the planner role and
exchanges JSON-line messages with it. The subprocess emits
``{"event": "ready"}`` once on startup, then for each plan task the
host writes a ``{"event": "plan", ...}`` line on stdin and reads
``{"event": "proposal", ...}`` lines back, terminated by either
``{"event": "plan-done", ...}`` or ``{"event": "plan-error", ...}``.

See ``docs/plans/eden-phase-10d-llm-worker-hosts.md`` §D.2 for the
wire format.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eden_contracts import PlanTask, Proposal
from eden_service_common import Subprocess, parse_json_line, spawn
from eden_storage import PlanSubmission, Store

log = logging.getLogger(__name__)

_HISTORY_LIMIT = 50
"""Cap on completed-trial history attached to each plan dispatch."""


@dataclass
class PlannerSubprocessConfig:
    """Knobs for the planner subprocess loop."""

    command: str
    cwd: Path
    env: Mapping[str, str]
    startup_deadline: float
    task_deadline: float
    shutdown_deadline: float


class ProtocolViolation(Exception):
    """Raised when the subprocess emits an unexpected line shape."""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _proposal_id() -> str:
    return f"proposal-{uuid.uuid4().hex[:12]}"


def _build_history(store: Store, *, limit: int = _HISTORY_LIMIT) -> list[dict]:
    """Pull the most recent completed trials from the event log.

    Returns at most ``limit`` entries, newest first. Each entry
    carries the trial's ``trial_id``, ``status``, ``commit_sha``, and
    ``metrics`` (from any ``trial.evaluated`` event), plus the
    proposal slug if the proposal can be located. The set is small
    by design — see §D.2 in the chunk plan.
    """
    history: list[dict] = []
    seen: set[str] = set()
    for event in reversed(store.read_range(0)):
        if event.type != "task.completed":
            continue
        if len(history) >= limit:
            break
        task_id = event.data.get("task_id")
        if not isinstance(task_id, str) or not task_id.startswith("evaluate-"):
            continue
        try:
            sub = store.read_submission(task_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            continue
        if sub is None:
            continue
        from eden_storage import EvaluateSubmission  # local — avoid cycle at import
        if not isinstance(sub, EvaluateSubmission):
            continue
        if sub.trial_id in seen:
            continue
        seen.add(sub.trial_id)
        try:
            trial = store.read_trial(sub.trial_id)
        except Exception:  # noqa: BLE001
            continue
        history.append(
            {
                "trial_id": trial.trial_id,
                "status": sub.status,
                "commit_sha": trial.commit_sha,
                "metrics": dict(sub.metrics or {}),
            }
        )
    return history


class PlannerSubprocess:
    """A live planner subprocess plus the protocol state machine."""

    def __init__(self, *, sub: Subprocess, config: PlannerSubprocessConfig) -> None:
        self._sub = sub
        self._config = config

    @property
    def is_alive(self) -> bool:
        """Is the underlying process still running?"""
        return self._sub.is_alive()

    def await_ready(self) -> None:
        """Block until the subprocess prints ``{"event": "ready"}``.

        Raises :class:`TimeoutError` after ``startup_deadline``.
        """
        deadline = time.monotonic() + self._config.startup_deadline
        while True:
            line = self._sub.read_line(deadline=deadline)
            if line is None:
                raise RuntimeError("planner subprocess exited before ready")
            obj = parse_json_line(line)
            if obj is None:
                log.debug("planner_pre_ready_noise", extra={"line": line})
                continue
            if obj.get("event") == "ready":
                log.info("planner_ready")
                return
            log.debug("planner_pre_ready_event", extra={"line": line})

    def dispatch_plan(self, *, task: PlanTask, history: list[dict], experiment_id: str,
                      objective: dict, metrics_schema: dict) -> tuple[dict, list[dict]]:
        """Send a plan dispatch and collect proposals until terminator.

        Returns a tuple ``(terminator_obj, proposal_dicts)`` where
        ``terminator_obj`` is the parsed terminator line (event is
        ``"plan-done"`` or ``"plan-error"``; ``plan-error`` may carry
        a ``reason`` field for diagnostics) and ``proposal_dicts`` is
        the list of proposal records.

        Raises :class:`ProtocolViolation` on bad shape;
        :class:`TimeoutError` on deadline; :class:`RuntimeError` on
        EOF.
        """
        dispatch = {
            "event": "plan",
            "task_id": task.task_id,
            "experiment_id": experiment_id,
            "objective": objective,
            "metrics_schema": metrics_schema,
            "history": history,
        }
        import json
        # Tag stderr forwarding with the active plan task so the
        # long-running planner subprocess's diagnostic lines are
        # attributable to the dispatch they were emitted under.
        self._sub.set_current_task(task.task_id)
        self._sub.write_line(json.dumps(dispatch, sort_keys=True))
        deadline = time.monotonic() + self._config.task_deadline
        proposals: list[dict] = []
        while True:
            line = self._sub.read_line(deadline=deadline)
            if line is None:
                raise RuntimeError("planner subprocess exited mid-task")
            obj = parse_json_line(line)
            if obj is None:
                log.debug("planner_noise_line", extra={"line": line})
                continue
            event = obj.get("event")
            if obj.get("task_id") != task.task_id:
                raise ProtocolViolation(
                    f"planner emitted line with task_id={obj.get('task_id')!r}; "
                    f"current dispatch is {task.task_id!r}"
                )
            if event == "proposal":
                proposals.append(obj)
                continue
            if event == "plan-done":
                return obj, proposals
            if event == "plan-error":
                return obj, proposals
            raise ProtocolViolation(f"planner emitted unknown event {event!r}")

    def stop(self) -> None:
        """Terminate the subprocess (SIGTERM ladder)."""
        self._sub.terminate(shutdown_deadline=self._config.shutdown_deadline)


def start_planner_subprocess(config: PlannerSubprocessConfig) -> PlannerSubprocess:
    """Spawn the planner subprocess and wait for the ``ready`` line."""
    sub = spawn(
        command=config.command,
        cwd=config.cwd,
        env=config.env,
        role="planner",
    )
    wrapper = PlannerSubprocess(sub=sub, config=config)
    try:
        wrapper.await_ready()
    except Exception:
        wrapper.stop()
        raise
    return wrapper


def _persist_proposals(
    store: Store,
    *,
    task: PlanTask,
    proposals: list[dict],
    artifacts_dir: Path,
) -> list[str]:
    """Persist proposal records and write rationale artifacts.

    Returns the list of proposal IDs in submission order.
    """
    ids: list[str] = []
    for record in proposals:
        proposal_id = _proposal_id()
        slug = record.get("slug")
        priority = record.get("priority")
        parents = record.get("parent_commits")
        artifacts_uri = record.get("artifacts_uri")
        rationale = record.get("rationale")
        if not isinstance(slug, str):
            raise ProtocolViolation(f"proposal missing slug: {record!r}")
        if not isinstance(priority, (int, float)):
            raise ProtocolViolation(f"proposal missing priority: {record!r}")
        if not isinstance(parents, list) or not parents:
            raise ProtocolViolation(f"proposal missing parent_commits: {record!r}")
        if isinstance(rationale, str) and rationale:
            artifacts_uri = _write_rationale(
                artifacts_dir=artifacts_dir,
                proposal_id=proposal_id,
                rationale=rationale,
            )
        if not isinstance(artifacts_uri, str) or not artifacts_uri:
            raise ProtocolViolation(
                f"proposal {slug!r} has no artifacts_uri and no rationale"
            )
        proposal = Proposal(
            proposal_id=proposal_id,
            experiment_id=store.experiment_id,
            slug=slug,
            priority=float(priority),
            parent_commits=list(parents),
            artifacts_uri=artifacts_uri,
            state="drafting",
            created_at=_now_iso(),
        )
        store.create_proposal(proposal)
        store.mark_proposal_ready(proposal_id)
        ids.append(proposal_id)
    return ids


def _write_rationale(
    *, artifacts_dir: Path, proposal_id: str, rationale: str
) -> str:
    """Write rationale.md and return its file:// URI."""
    target_dir = artifacts_dir / "proposals" / proposal_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "rationale.md"
    target.write_text(rationale, encoding="utf-8")
    return target.resolve().as_uri()


def handle_plan_task(
    *,
    store: Store,
    task: PlanTask,
    worker_id: str,
    planner: PlannerSubprocess,
    experiment_id: str,
    objective: dict,
    metrics_schema: dict,
    artifacts_dir: Path,
) -> None:
    """Drive one plan task through the subprocess: claim → dispatch → submit."""
    claim = store.claim(task.task_id, worker_id)
    history = _build_history(store)
    try:
        terminator, proposals = planner.dispatch_plan(
            task=task,
            history=history,
            experiment_id=experiment_id,
            objective=objective,
            metrics_schema=metrics_schema,
        )
    except (ProtocolViolation, TimeoutError, RuntimeError) as exc:
        log.warning(
            "planner_dispatch_failed",
            extra={"task_id": task.task_id, "error": str(exc)},
        )
        store.submit(task.task_id, claim.token, PlanSubmission(status="error"))
        raise
    if terminator.get("event") == "plan-error":
        log.warning(
            "planner_plan_error",
            extra={
                "task_id": task.task_id,
                "reason": terminator.get("reason"),
                "proposals_seen": len(proposals),
            },
        )
        store.submit(task.task_id, claim.token, PlanSubmission(status="error"))
        return
    try:
        ids = _persist_proposals(
            store, task=task, proposals=proposals, artifacts_dir=artifacts_dir
        )
    except ProtocolViolation as exc:
        log.warning(
            "planner_proposal_invalid",
            extra={"task_id": task.task_id, "error": str(exc)},
        )
        store.submit(task.task_id, claim.token, PlanSubmission(status="error"))
        return
    store.submit(
        task.task_id,
        claim.token,
        PlanSubmission(status="success", proposal_ids=tuple(ids)),
    )


