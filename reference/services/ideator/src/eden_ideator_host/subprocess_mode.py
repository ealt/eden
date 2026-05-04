"""Phase 10d ideator-host subprocess mode.

Runs a single long-running subprocess for the ideator role and
exchanges JSON-line messages with it. The subprocess emits
``{"event": "ready"}`` once on startup, then for each ideate task the
host writes a ``{"event": "ideate", ...}`` line on stdin and reads
``{"event": "idea", ...}`` lines back, terminated by either
``{"event": "ideate-done", ...}`` or ``{"event": "ideate-error", ...}``.

See ``docs/plans/eden-phase-10d-llm-worker-hosts.md`` §D.2 for the
wire format.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eden_contracts import Idea, IdeateTask
from eden_service_common import Subprocess, parse_json_line, spawn
from eden_storage import IdeateSubmission, Store

log = logging.getLogger(__name__)

_HISTORY_LIMIT = 50
"""Cap on completed-variant history attached to each ideate dispatch."""


WrapResult = tuple[str, Callable[[], None] | None, list[Callable[[], None]]]
"""Result of a per-spawn ``wrap_factory`` call.

Tuple of (wrapped command, post_kill_callback, cleanup_callbacks).
The post_kill_callback runs after SIGKILL escalation; cleanup_callbacks
run on every terminal exit branch. Both are forwarded into ``spawn``.
"""


@dataclass
class IdeatorSubprocessConfig:
    """Knobs for the ideator subprocess loop."""

    command: str
    cwd: Path
    env: Mapping[str, str]
    startup_deadline: float
    task_deadline: float
    shutdown_deadline: float
    wrap_factory: Callable[[], WrapResult] | None = None
    """Optional factory invoked once per spawn (i.e. per host restart
    cycle) to build a wrapped command + per-spawn cleanup callbacks.

    When set, it takes precedence over ``command`` — useful for
    ``--exec-mode=docker`` where each spawn needs a fresh cidfile.
    The ``command`` field still must be set (used as a fallback +
    for log diagnostics)."""


class ProtocolViolation(Exception):
    """Raised when the subprocess emits an unexpected line shape."""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _idea_id() -> str:
    return f"idea-{uuid.uuid4().hex[:12]}"


def _build_history(store: Store, *, limit: int = _HISTORY_LIMIT) -> list[dict]:
    """Pull the most recent completed variants from the event log.

    Returns at most ``limit`` entries, newest first. Each entry
    carries the variant's ``variant_id``, ``status``, ``commit_sha``, and
    ``metrics`` (from any ``variant.evaluated`` event), plus the
    idea slug if the idea can be located. The set is small
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
        if sub.variant_id in seen:
            continue
        seen.add(sub.variant_id)
        try:
            variant = store.read_variant(sub.variant_id)
        except Exception:  # noqa: BLE001
            continue
        history.append(
            {
                "variant_id": variant.variant_id,
                "status": sub.status,
                "commit_sha": variant.commit_sha,
                "evaluation": dict(sub.evaluation or {}),
            }
        )
    return history


class IdeatorSubprocess:
    """A live ideator subprocess plus the protocol state machine."""

    def __init__(self, *, sub: Subprocess, config: IdeatorSubprocessConfig) -> None:
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
                raise RuntimeError("ideator subprocess exited before ready")
            obj = parse_json_line(line)
            if obj is None:
                log.debug("ideator_pre_ready_noise", extra={"line": line})
                continue
            if obj.get("event") == "ready":
                log.info("ideator_ready")
                return
            log.debug("ideator_pre_ready_event", extra={"line": line})

    def dispatch_plan(self, *, task: IdeateTask, history: list[dict], experiment_id: str,
                      objective: dict, evaluation_schema: dict) -> tuple[dict, list[dict]]:
        """Send a ideate dispatch and collect ideas until terminator.

        Returns a tuple ``(terminator_obj, idea_dicts)`` where
        ``terminator_obj`` is the parsed terminator line (event is
        ``"ideate-done"`` or ``"ideate-error"``; ``plan-error`` may carry
        a ``reason`` field for diagnostics) and ``idea_dicts`` is
        the list of idea records.

        Raises :class:`ProtocolViolation` on bad shape;
        :class:`TimeoutError` on deadline; :class:`RuntimeError` on
        EOF.
        """
        dispatch = {
            "event": "ideate",
            "task_id": task.task_id,
            "experiment_id": experiment_id,
            "objective": objective,
            "evaluation_schema": evaluation_schema,
            "history": history,
        }
        import json
        # Tag stderr forwarding with the active ideate task so the
        # long-running ideator subprocess's diagnostic lines are
        # attributable to the dispatch they were emitted under.
        self._sub.set_current_task(task.task_id)
        self._sub.write_line(json.dumps(dispatch, sort_keys=True))
        deadline = time.monotonic() + self._config.task_deadline
        ideas: list[dict] = []
        while True:
            line = self._sub.read_line(deadline=deadline)
            if line is None:
                raise RuntimeError("ideator subprocess exited mid-task")
            obj = parse_json_line(line)
            if obj is None:
                log.debug("ideator_noise_line", extra={"line": line})
                continue
            event = obj.get("event")
            if obj.get("task_id") != task.task_id:
                raise ProtocolViolation(
                    f"ideator emitted line with task_id={obj.get('task_id')!r}; "
                    f"current dispatch is {task.task_id!r}"
                )
            if event == "idea":
                ideas.append(obj)
                continue
            if event == "ideate-done":
                return obj, ideas
            if event == "ideate-error":
                return obj, ideas
            raise ProtocolViolation(f"ideator emitted unknown event {event!r}")

    def stop(self) -> None:
        """Terminate the subprocess (SIGTERM ladder)."""
        self._sub.terminate(shutdown_deadline=self._config.shutdown_deadline)


def start_ideator_subprocess(config: IdeatorSubprocessConfig) -> IdeatorSubprocess:
    """Spawn the ideator subprocess and wait for the ``ready`` line."""
    if config.wrap_factory is not None:
        command, post_kill, cleanups = config.wrap_factory()
    else:
        command, post_kill, cleanups = config.command, None, []
    sub = spawn(
        command=command,
        cwd=config.cwd,
        env=config.env,
        role="ideator",
        post_kill_callback=post_kill,
        cleanup_callbacks=cleanups,
    )
    wrapper = IdeatorSubprocess(sub=sub, config=config)
    try:
        wrapper.await_ready()
    except Exception:
        wrapper.stop()
        raise
    return wrapper


def _persist_ideas(
    store: Store,
    *,
    task: IdeateTask,
    ideas: list[dict],
    artifacts_dir: Path,
) -> list[str]:
    """Persist idea records and write rationale artifacts.

    Returns the list of idea IDs in submission order.
    """
    ids: list[str] = []
    for record in ideas:
        idea_id = _idea_id()
        slug = record.get("slug")
        priority = record.get("priority")
        parents = record.get("parent_commits")
        artifacts_uri = record.get("artifacts_uri")
        rationale = record.get("rationale")
        if not isinstance(slug, str):
            raise ProtocolViolation(f"idea missing slug: {record!r}")
        if not isinstance(priority, (int, float)):
            raise ProtocolViolation(f"idea missing priority: {record!r}")
        if not isinstance(parents, list) or not parents:
            raise ProtocolViolation(f"idea missing parent_commits: {record!r}")
        if isinstance(rationale, str) and rationale:
            artifacts_uri = _write_rationale(
                artifacts_dir=artifacts_dir,
                idea_id=idea_id,
                rationale=rationale,
            )
        if not isinstance(artifacts_uri, str) or not artifacts_uri:
            raise ProtocolViolation(
                f"idea {slug!r} has no artifacts_uri and no rationale"
            )
        idea = Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug=slug,
            priority=float(priority),
            parent_commits=list(parents),
            artifacts_uri=artifacts_uri,
            state="drafting",
            created_at=_now_iso(),
        )
        store.create_idea(idea)
        store.mark_idea_ready(idea_id)
        ids.append(idea_id)
    return ids


def _write_rationale(
    *, artifacts_dir: Path, idea_id: str, rationale: str
) -> str:
    """Write rationale.md and return its file:// URI."""
    target_dir = artifacts_dir / "ideas" / idea_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "rationale.md"
    target.write_text(rationale, encoding="utf-8")
    return target.resolve().as_uri()


def handle_plan_task(
    *,
    store: Store,
    task: IdeateTask,
    worker_id: str,
    ideator: IdeatorSubprocess,
    experiment_id: str,
    objective: dict,
    evaluation_schema: dict,
    artifacts_dir: Path,
) -> None:
    """Drive one ideate task through the subprocess: claim → dispatch → submit."""
    claim = store.claim(task.task_id, worker_id)
    history = _build_history(store)
    try:
        terminator, ideas = ideator.dispatch_plan(
            task=task,
            history=history,
            experiment_id=experiment_id,
            objective=objective,
            evaluation_schema=evaluation_schema,
        )
    except (ProtocolViolation, TimeoutError, RuntimeError) as exc:
        log.warning(
            "ideator_dispatch_failed",
            extra={"task_id": task.task_id, "error": str(exc)},
        )
        store.submit(task.task_id, claim.token, IdeateSubmission(status="error"))
        raise
    if terminator.get("event") == "ideate-error":
        log.warning(
            "ideator_ideate_error",
            extra={
                "task_id": task.task_id,
                "reason": terminator.get("reason"),
                "ideas_seen": len(ideas),
            },
        )
        store.submit(task.task_id, claim.token, IdeateSubmission(status="error"))
        return
    try:
        ids = _persist_ideas(
            store, task=task, ideas=ideas, artifacts_dir=artifacts_dir
        )
    except ProtocolViolation as exc:
        log.warning(
            "ideator_idea_invalid",
            extra={"task_id": task.task_id, "error": str(exc)},
        )
        store.submit(task.task_id, claim.token, IdeateSubmission(status="error"))
        return
    store.submit(
        task.task_id,
        claim.token,
        IdeateSubmission(status="success", idea_ids=tuple(ids)),
    )


