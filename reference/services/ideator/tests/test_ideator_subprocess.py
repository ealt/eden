"""Unit tests for the ideator subprocess mode."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema, ExperimentConfig, IdeationTask, ObjectiveSpec
from eden_ideator_host import build_subprocess_config, run_ideator_subprocess_loop
from eden_ideator_host.subprocess_mode import (
    IdeatorSubprocessConfig,
    ProtocolViolation,
    handle_ideation_task,
    start_ideator_subprocess,
)
from eden_service_common import StopFlag, seed_bare_repo
from eden_storage import IdeaSubmission, InMemoryStore

EXPERIMENT_ID = "exp_0123456789abcdefghjkmnpqrs"


def _seed_store_and_repo(tmp_path: Path) -> tuple[InMemoryStore, str, str]:
    repo_path = tmp_path / "bare.git"
    from eden_git import GitRepo

    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    store = InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
    )
    # Issue #128: worker_ids are now system-minted/opaque. Mint the
    # ideator worker the subprocess loop uses (Store.claim's §3.5
    # step-2 registration check rejects unregistered worker_ids) and
    # return its id so callers thread the minted claimant.
    _w, _ = store.register_worker(name="ideator-1")
    ideator_id = _w.worker_id
    return store, seed_sha, ideator_id


def _experiment_config() -> ExperimentConfig:
    return ExperimentConfig(
        parallel_variants=1,
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
        objective=ObjectiveSpec(expr="score", direction="maximize"),
    )


def _write_worker(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "worker.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config(
    *, command: str, cwd: Path, startup: float = 5, task: float = 5
) -> IdeatorSubprocessConfig:
    return build_subprocess_config(
        command=command,
        cwd=cwd,
        env={},
        startup_deadline=startup,
        task_deadline=task,
        shutdown_deadline=2,
    )


def test_ready_handshake(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        sys.stdin.readline()
        """,
    )
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    assert sub.is_alive
    sub.stop()


def test_dispatch_collects_ideas(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        task_id = dispatch["task_id"]
        for i in range(2):
            print(json.dumps({"event": "idea", "task_id": task_id,
                              "slug": f"{task_id}-p{i}",
                              "priority": float(2 - i),
                              "parent_commits": ["a" * 40],
                              "content": f"# content {i}\\n"}),
                  flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    handle_ideation_task(
        store=store,
        task=ideation_task,
        worker_id=ideator_id,
        ideator=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        evaluation_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submitted = store.read_task("ideation-1")
    assert submitted.state == "submitted"
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "success"
    assert len(submission.idea_ids) == 2
    ideas = [store.read_idea(pid) for pid in submission.idea_ids]
    assert all(p.state == "ready" for p in ideas)
    # Issue #168: content lands at ideas/<idea_id>/content.md on disk.
    for p in ideas:
        assert p.artifacts_uri.endswith(f"/ideas/{p.idea_id}/content.md")
        assert (artifacts / "ideas" / p.idea_id / "content.md").is_file()


def test_whitespace_only_content_errors_not_stuck(tmp_path: Path) -> None:
    """Whitespace-only content + no artifacts_uri must submit status=error.

    Issue #168 regression: the shared writer strips and rejects whitespace-only
    text with ValueError (not ProtocolViolation). If _persist_ideas let that
    through it would be uncaught and leave the task stuck claimed. The host must
    treat whitespace-only as "no content" → ProtocolViolation → error.
    """
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        task_id = dispatch["task_id"]
        print(json.dumps({"event": "idea", "task_id": task_id,
                          "slug": f"{task_id}-p0", "priority": 1.0,
                          "parent_commits": ["a" * 40],
                          "content": "   \\n\\t  "}), flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    # Must NOT raise — the ValueError-vs-ProtocolViolation gap is closed.
    handle_ideation_task(
        store=store,
        task=ideation_task,
        worker_id=ideator_id,
        ideator=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        evaluation_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "error"


def test_ideation_error_terminator(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        print(json.dumps({
            "event": "ideation-error",
            "task_id": dispatch["task_id"],
            "reason": "no",
        }), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    handle_ideation_task(
        store=store,
        task=ideation_task,
        worker_id=ideator_id,
        ideator=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        evaluation_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "error"


def test_protocol_violation_wrong_task_id(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        # Reply with a different task_id (protocol violation).
        print(json.dumps({"event": "idea", "task_id": "nope",
                          "slug": "x", "priority": 1.0,
                          "parent_commits": ["a" * 40]}),
              flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    with pytest.raises(ProtocolViolation):
        handle_ideation_task(
            store=store,
            task=ideation_task,
            worker_id=ideator_id,
            ideator=sub,
            experiment_id=EXPERIMENT_ID,
            objective={"expr": "score", "direction": "maximize"},
            evaluation_schema={"score": "real"},
            artifacts_dir=tmp_path,
        )
    sub.stop()
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "error"


def test_loop_respawns_on_subprocess_crash(tmp_path: Path) -> None:
    """When the subprocess exits unexpectedly, the loop respawns it."""
    worker = _write_worker(
        tmp_path,
        """
        import json, os, sys
        print(json.dumps({"event": "ready"}), flush=True)
        marker = os.environ.get("CRASH_MARKER")
        if marker and not os.path.exists(marker):
            open(marker, "w").close()
            sys.exit(1)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        task_id = dispatch["task_id"]
        print(json.dumps({"event": "idea", "task_id": task_id,
                          "slug": "p0", "priority": 1.0,
                          "parent_commits": ["a" * 40],
                          "content": "# r"}),
              flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    store.create_ideation_task("ideation-2")
    marker = tmp_path / "crashed"
    config = build_subprocess_config(
        command=f"python3 {worker}",
        cwd=tmp_path,
        env={"CRASH_MARKER": str(marker)},
        startup_deadline=5,
        task_deadline=5,
        shutdown_deadline=2,
    )
    stop = StopFlag()

    import threading

    def _run() -> None:
        run_ideator_subprocess_loop(
            store=store,
            worker_id=ideator_id,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_experiment_config(),
            artifacts_dir=tmp_path / "artifacts",
            subprocess_config=config,
            poll_interval=0.05,
            stop=stop,
        )

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        t1 = store.read_task("ideation-1")
        t2 = store.read_task("ideation-2")
        if t1.state == "submitted" and t2.state == "submitted":
            break
        time.sleep(0.1)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert marker.is_file()
    # First task got submit-error from the crash; second succeeds after respawn.
    s1 = store.read_submission("ideation-1")
    s2 = store.read_submission("ideation-2")
    assert isinstance(s1, IdeaSubmission)
    assert isinstance(s2, IdeaSubmission)
    assert s1.status == "error"
    assert s2.status == "success"


# ----------------------------------------------------------------------
# Cost capture (issue #343)
# ----------------------------------------------------------------------


def _drive_one_ideation(
    store: InMemoryStore, ideator_id: str, tmp_path: Path, worker: Path
) -> None:
    store.create_ideation_task("ideation-1")
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    handle_ideation_task(
        store=store,
        task=ideation_task,
        worker_id=ideator_id,
        ideator=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        evaluation_schema={"score": "real"},
        artifacts_dir=tmp_path / "artifacts",
    )
    sub.stop()


def test_terminator_cost_is_recorded(tmp_path: Path) -> None:
    """A gateway bridge reports normalized usage on ``ideation-done``."""
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        task_id = dispatch["task_id"]
        print(json.dumps({"event": "idea", "task_id": task_id,
                          "slug": "p0", "priority": 1.0,
                          "parent_commits": ["a" * 40],
                          "content": "# c\\n"}), flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id,
                          "cost": {"input_tokens": 4200,
                                   "output_tokens": 830,
                                   "total_cost_usd": 0.21,
                                   "model": "claude-fable-5"}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    _drive_one_ideation(store, ideator_id, tmp_path, worker)

    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "success"

    (entry,) = store.list_cost_entries()
    assert entry.role == "ideator"
    assert entry.task_id == "ideation-1"
    assert entry.source == "worker-reported"
    assert entry.input_tokens == 4200
    assert entry.output_tokens == 830
    assert entry.total_cost_usd == 0.21
    assert entry.model == "claude-fable-5"
    # No variant exists yet at ideation time; per-idea attribution is
    # the idea_ids on the submission, not a variant.
    assert entry.variant_id is None


def test_failed_ideation_still_records_its_spend(tmp_path: Path) -> None:
    """An ideation-error attempt burned gateway tokens all the same."""
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ideation-error",
                          "task_id": dispatch["task_id"],
                          "reason": "gateway returned no parseable ideas",
                          "cost": {"input_tokens": 4200,
                                   "output_tokens": 12}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    _drive_one_ideation(store, ideator_id, tmp_path, worker)

    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "error"

    (entry,) = store.list_cost_entries()
    assert entry.role == "ideator"
    assert entry.input_tokens == 4200
    assert entry.total_cost_usd is None


def test_terminator_without_cost_records_nothing(tmp_path: Path) -> None:
    """The key is optional; a bridge that reports nothing changes nothing."""
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ideation-done",
                          "task_id": dispatch["task_id"]}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    _drive_one_ideation(store, ideator_id, tmp_path, worker)
    assert store.list_cost_entries() == []


def test_two_dispatches_of_one_task_each_record(tmp_path: Path) -> None:
    """A reclaimed-and-re-dispatched task spent twice; both rows land.

    The ideator's attempt key is a per-dispatch nonce (an ideation
    dispatch has no stable per-attempt id), so this asserts the nonce
    actually separates dispatches rather than collapsing them.
    """
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            dispatch = json.loads(line)
            print(json.dumps({"event": "ideation-error",
                              "task_id": dispatch["task_id"],
                              "cost": {"total_cost_usd": 0.1}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    for n in (1, 2):
        store.create_ideation_task(f"ideation-{n}")
        task = store.read_task(f"ideation-{n}")
        assert isinstance(task, IdeationTask)
        handle_ideation_task(
            store=store,
            task=task,
            worker_id=ideator_id,
            ideator=sub,
            experiment_id=EXPERIMENT_ID,
            objective={"expr": "score", "direction": "maximize"},
            evaluation_schema={"score": "real"},
            artifacts_dir=tmp_path / "artifacts",
        )
    sub.stop()

    entries = store.list_cost_entries()
    assert len(entries) == 2
    assert sum(e.total_cost_usd or 0.0 for e in entries) == 0.2


def test_single_idea_dispatch_attributes_cost_to_that_idea(
    tmp_path: Path,
) -> None:
    """One idea from one dispatch → unambiguous per-idea attribution.

    This is the common case (the R3 bridge emits one idea per dispatch),
    and it is what makes "which ideas cost what" answerable at all.
    """
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        task_id = dispatch["task_id"]
        print(json.dumps({"event": "idea", "task_id": task_id,
                          "slug": "p0", "priority": 1.0,
                          "parent_commits": ["a" * 40],
                          "content": "# c\\n"}), flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id,
                          "cost": {"total_cost_usd": 0.21}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    _drive_one_ideation(store, ideator_id, tmp_path, worker)

    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    (idea_id,) = submission.idea_ids
    (entry,) = store.list_cost_entries()
    assert entry.idea_id == idea_id


def test_multi_idea_dispatch_is_attributed_to_no_single_idea(
    tmp_path: Path,
) -> None:
    """One indivisible gateway call produced three ideas.

    Picking one, or splitting the cost three ways, would both be
    inventions — so the entry stays at role/task level and the rollup
    counts it as unattributed.
    """
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        task_id = dispatch["task_id"]
        for i in range(3):
            print(json.dumps({"event": "idea", "task_id": task_id,
                              "slug": f"p{i}", "priority": 1.0,
                              "parent_commits": ["a" * 40],
                              "content": f"# c{i}\\n"}), flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id,
                          "cost": {"total_cost_usd": 0.6}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    _drive_one_ideation(store, ideator_id, tmp_path, worker)

    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert len(submission.idea_ids) == 3
    (entry,) = store.list_cost_entries()
    assert entry.idea_id is None
    assert entry.total_cost_usd == 0.6


def test_failed_dispatch_records_without_an_idea(tmp_path: Path) -> None:
    """No idea exists to attribute to, but the spend still lands."""
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ideation-error",
                          "task_id": dispatch["task_id"],
                          "cost": {"total_cost_usd": 0.3}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)
    _drive_one_ideation(store, ideator_id, tmp_path, worker)

    (entry,) = store.list_cost_entries()
    assert entry.idea_id is None
    assert entry.total_cost_usd == 0.3


def test_dispatch_nonce_is_minted_before_the_dispatch(tmp_path: Path) -> None:
    """The cost key is reconstructible for the life of a dispatch.

    Round-1 finding: minting the nonce inside the record call made the
    ideator the one role whose idempotency key a retry could not
    reproduce. Nothing retries that call today, so this is hardening
    rather than a live double-count — asserted by driving the real
    handler and checking the recorded key against the nonce the host
    generated.
    """
    import eden_ideator_host.subprocess_mode as mod

    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        dispatch = json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ideation-error",
                          "task_id": dispatch["task_id"],
                          "cost": {"total_cost_usd": 0.2}}), flush=True)
        """,
    )
    store, _, ideator_id = _seed_store_and_repo(tmp_path)

    seen: list[str] = []
    real = mod._record_ideation_cost

    def _spy(**kwargs: object) -> None:
        seen.append(str(kwargs["dispatch_nonce"]))
        real(**kwargs)  # type: ignore[arg-type]

    mod._record_ideation_cost = _spy
    try:
        _drive_one_ideation(store, ideator_id, tmp_path, worker)
    finally:
        mod._record_ideation_cost = real

    (nonce,) = seen
    (entry,) = store.list_cost_entries()
    # The key is derived from that exact nonce, so a retry holding the
    # same dispatch state reproduces it.
    from eden_storage import composite_attempt_key, cost_entry_id

    assert entry.entry_id == cost_entry_id(
        role="ideator",
        attempt_key=composite_attempt_key("ideation-1", nonce),
    )
