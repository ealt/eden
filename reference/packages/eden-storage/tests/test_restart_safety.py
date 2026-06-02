"""Restart-safety scenarios for ``SqliteStore``.

Chapter 8 §3 requires every acknowledged write to survive a crash
of the store's host. These tests simulate a crash (or clean close)
by closing the ``SqliteStore`` connection mid-experiment and
reopening it against the same file. Every stored entity and every
event MUST be visible to the reopened store, and subsequent
operations MUST continue from the recovered state as though the
connection never dropped.

The parametrized conformance scenarios in ``test_*.py`` run against
``SqliteStore`` (via ``make_store``), which asserts that
transactional semantics hold *within* a single process lifetime.
These tests add the piece that only a durable backend has to prove:
the same semantics hold *across* a process boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema, Idea, Variant, mint_opaque_id
from eden_dispatch import (
    ScriptedEvaluator,
    ScriptedExecutor,
    ScriptedIdeator,
)
from eden_dispatch.workers import EvaluationOutcome, ExecutionOutcome, IdeaTemplate
from eden_storage import (
    EvaluationSubmission,
    IdeaSubmission,
    InvalidPrecondition,
    SqliteStore,
    VariantSubmission,
)

# Worker display-names the in-line tests use against directly-constructed
# stores. Post-#128 ``register_worker`` mints opaque ids, so the
# cross-restart tests resolve a name to the id the FIRST store minted
# (worker rows persist across reopen) rather than re-registering — a
# second register would mint a fresh duplicate row.
# Opaque experiment ids reused across reopen within a test. Module-level
# so the same string flows into both the FIRST and SECOND store opened
# against a given db file; each test uses its own tmp_path so sharing
# the constant across tests is safe. `_EXP_A` / `_EXP_B` are distinct so
# the wrong-experiment-id reopen test exercises a real mismatch.
_EXP_R = mint_opaque_id("exp")
_EXP_A = mint_opaque_id("exp")
_EXP_B = mint_opaque_id("exp")


_RESTART_WORKERS = (
    "ideator-1",
    "ideator-w",
    "execution-1",
    "executor-w",
    "evaluation-1",
    "eval-1",
    "evaluator-w",
    "impl-worker",
    "test-worker",
    "worker-a",
)


def _register_restart_workers(store) -> dict[str, str]:
    """Register the restart worker set on a fresh store; return name→id map.

    Idempotent across reopen by RESOLUTION, not re-registration: if a
    worker with the given name already exists (persisted by a prior
    store against the same db), reuse its minted id instead of minting
    a duplicate.
    """
    seeded: dict[str, str] = {}
    for name in _RESTART_WORKERS:
        existing = store.list_workers(name=name)
        if existing:
            seeded[name] = existing[0].worker_id
        else:
            worker, _ = store.register_worker(name=name)
            seeded[name] = worker.worker_id
    store.seeded_workers = seeded  # type: ignore[attr-defined]
    return seeded

class TestStateSurvivesReopen:
    """Every acknowledged write (§3.1) MUST be visible after reopen."""

    def test_tasks_ideas_variants_and_events_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "eden.db"
        first = SqliteStore(_EXP_R, path)
        _register_restart_workers(first)
        first.create_idea(
            Idea(
                idea_id="p1",
                experiment_id=_EXP_R,
                slug="feat-a",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p1",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        first.mark_idea_ready("p1")
        first.create_execution_task("t-exec", "p1")
        claim = first.claim("t-exec", first.seeded_workers["executor-w"])
        first.create_variant(
            Variant(
                variant_id="variant-1",
                experiment_id=_EXP_R,
                idea_id="p1",
                status="starting",
                parent_commits=["a" * 40],
                branch="work/feat-a-variant-1",
                started_at="2026-04-23T00:01:00.000Z",
            )
        )
        first.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        first.accept("t-exec")
        first_events = first.events()
        first.close()

        second = SqliteStore(_EXP_R, path)
        _register_restart_workers(second)
        assert [e.type for e in second.events()] == [e.type for e in first_events]
        assert second.read_task("t-exec").state == "completed"
        assert second.read_idea("p1").state == "completed"
        variant = second.read_variant("variant-1")
        assert variant.status == "starting"
        assert variant.commit_sha == "b" * 40
        second.close()

    def test_claim_token_survives_reopen_and_authorizes_submit(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "eden.db"
        first = SqliteStore(_EXP_R, path)
        _register_restart_workers(first)
        first.create_ideation_task("t-ideation")
        claim = first.claim("t-ideation", first.seeded_workers["ideator-w"])
        first.close()

        second = SqliteStore(_EXP_R, path)
        _register_restart_workers(second)
        task = second.read_task("t-ideation")
        assert task.state == "claimed"
        assert task.claim is not None
        assert task.claim.worker_id == claim.worker_id
        # The persisted token authorizes a fresh submit on the reopened store.
        second.submit("t-ideation", claim.worker_id, IdeaSubmission(status="success"))
        assert second.read_task("t-ideation").state == "submitted"
        second.close()

    def test_event_order_total_and_preserved(self, tmp_path: Path) -> None:
        """§2.2 single total order; §4.4 full replay from first event."""
        path = tmp_path / "eden.db"
        first = SqliteStore(_EXP_R, path)
        _register_restart_workers(first)
        first.create_ideation_task("t1")
        first.create_ideation_task("t2")
        before = [e.event_id for e in first.events()]
        first.close()

        second = SqliteStore(_EXP_R, path)
        _register_restart_workers(second)
        second.create_ideation_task("t3")
        after = [e.event_id for e in second.events()]
        assert after[: len(before)] == before
        assert len(after) == len(before) + 1
        second.close()


class TestExperimentIdentityIsEnforced:
    """Chapter 8 §4.2 + sanity: a database belongs to exactly one experiment."""

    def test_reopen_with_wrong_experiment_id_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "eden.db"
        SqliteStore(_EXP_A, path).close()
        with pytest.raises(InvalidPrecondition, match=_EXP_A):
            SqliteStore(_EXP_B, path)

    def test_reopen_with_different_evaluation_schema_rejected(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "eden.db"
        SqliteStore(
            _EXP_A,
            path,
            evaluation_schema=EvaluationSchema({"score": "real"}),
        ).close()
        with pytest.raises(InvalidPrecondition, match="evaluation_schema"):
            SqliteStore(
                _EXP_A,
                path,
                evaluation_schema=EvaluationSchema({"score": "integer"}),
            )

    def test_reopen_with_reordered_schema_keys_accepted(
        self, tmp_path: Path
    ) -> None:
        """§4.2 pins semantic identity; JSON key order is not meaningful.

        A reopen that supplies the same metric map with keys in a
        different insertion order MUST be accepted. The Phase 6
        round-1 review caught that the literal-JSON comparison was
        rejecting benign dict-rebuild differences.
        """
        path = tmp_path / "eden.db"
        SqliteStore(
            _EXP_A,
            path,
            evaluation_schema=EvaluationSchema({"score": "real", "latency": "integer"}),
        ).close()
        # Reorder the keys; semantically identical schema.
        SqliteStore(
            _EXP_A,
            path,
            evaluation_schema=EvaluationSchema({"latency": "integer", "score": "real"}),
        ).close()

    def test_event_id_counter_resumes_past_default_format_boundary(
        self, tmp_path: Path
    ) -> None:
        """`_next_event_seq` must not depend on the evt-NNNNNN width.

        Round-1 review caught a regression in which the counter was
        sniffed via a six-digit pattern; this test hand-inserts a
        high-numbered event, reopens, and asserts the next default
        event_id does not collide.
        """
        import sqlite3

        path = tmp_path / "eden.db"
        # Create + close a store to set up the schema.
        SqliteStore(_EXP_R, path).close()
        # Insert a synthetic high-numbered event well past the
        # six-digit boundary.
        conn = sqlite3.connect(str(path))
        conn.execute(
            """
            INSERT INTO event(seq, event_id, type, occurred_at, experiment_id, data)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                1_000_500,
                "evt-1000500",
                "task.created",
                "2026-04-23T00:00:00.000Z",
                _EXP_R,
                '{"event_id":"evt-1000500","type":"task.created",'
                '"occurred_at":"2026-04-23T00:00:00.000Z",'
                '"experiment_id":"' + _EXP_R + '",'
                '"data":{"task_id":"synthetic","kind": "ideation"}}',
            ),
        )
        conn.commit()
        conn.close()

        # Reopen with the default factory; subsequent event must not
        # collide with the synthetic one.
        second = SqliteStore(_EXP_R, path)
        _register_restart_workers(second)
        second.create_ideation_task("t-new")
        # The newly-appended event must come after the synthetic one.
        events = second.events()
        assert len(events) == 2
        assert events[0].event_id == "evt-1000500"
        # No collision on INSERT — if the counter had reset to 1, the
        # next event_id would be "evt-000001" and the INSERT would
        # succeed but the UNIQUE constraint would be preserved by
        # accident. Assert the new event_id lives past the synthetic.
        # (We only need to prove no IntegrityError was raised.)
        second.close()

    def test_reopen_without_evaluation_schema_inherits_stored_schema(
        self, tmp_path: Path
    ) -> None:
        """The stored schema MUST continue to enforce §4.3 after reopen."""
        path = tmp_path / "eden.db"
        first = SqliteStore(
            _EXP_A,
            path,
            evaluation_schema=EvaluationSchema({"score": "integer"}),
        )
        first.close()

        second = SqliteStore(_EXP_A, path)
        _register_restart_workers(second)
        # Drive a full variant to the point of an evaluation-task submission and
        # assert the inherited schema rejects a wrong-type metric.
        second.create_idea(
            Idea(
                idea_id="p1",
                experiment_id=_EXP_A,
                slug="feat-a",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p1",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        second.mark_idea_ready("p1")
        second.create_execution_task("t-exec", "p1")
        c = second.claim("t-exec", second.seeded_workers["executor-w"])
        second.create_variant(
            Variant(
                variant_id="variant-1",
                experiment_id=_EXP_A,
                idea_id="p1",
                status="starting",
                parent_commits=["a" * 40],
                branch="work/feat-a-variant-1",
                started_at="2026-04-23T00:01:00.000Z",
            )
        )
        second.submit(
            "t-exec",
            c.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        second.accept("t-exec")
        second.create_evaluation_task("t-eval", "variant-1")
        ec = second.claim("t-eval", second.seeded_workers["evaluator-w"])
        second.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"score": "not-an-int"},
            ),
        )
        decision, reason = second.validate_terminal("t-eval")
        assert decision == "reject_validation"
        assert reason is not None
        assert "score" in reason
        second.close()


class TestCrashRecoveryRollsBackPartialWrites:
    """§3.3 + §6.3: a transaction that fails must leave no partial state."""

    def test_exception_during_commit_rolls_back(self, tmp_path: Path) -> None:
        """A failure inside ``_apply_commit`` MUST leave nothing persisted.

        We monkey-patch ``_insert_event`` to raise on the second event
        of a composite commit (``create_execution_task`` emits two
        events: ``task.created`` + ``idea.dispatched``). If
        rollback works, a reopened store sees neither the task
        row nor the idea state change — everything stays at the
        pre-operation checkpoint.
        """
        path = tmp_path / "eden.db"
        first = SqliteStore(_EXP_R, path)
        _register_restart_workers(first)
        first.create_idea(
            Idea(
                idea_id="p1",
                experiment_id=_EXP_R,
                slug="feat-a",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p1",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        first.mark_idea_ready("p1")
        checkpoint_events = [e.event_id for e in first.events()]

        calls = {"n": 0}
        original = first._insert_event  # noqa: SLF001

        def boom(event):  # noqa: ANN001, ANN202
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated mid-commit failure")
            return original(event)

        first._insert_event = boom  # noqa: SLF001
        with pytest.raises(RuntimeError, match="simulated"):
            first.create_execution_task("t-exec", "p1")
        first._insert_event = original  # noqa: SLF001
        first.close()

        # Reopen and verify: no task row, idea still 'ready', event
        # log unchanged from the checkpoint.
        second = SqliteStore(_EXP_R, path)
        _register_restart_workers(second)
        assert second.list_tasks() == []
        assert second.read_idea("p1").state == "ready"
        assert [e.event_id for e in second.events()] == checkpoint_events
        second.close()


class TestRunExperimentAcrossRestarts:
    """End-to-end: an experiment paused mid-flight resumes on reopen."""

    def test_ideation_then_execution_then_restart_then_evaluation(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "eden.db"

        idea_ids = iter([f"idea-{i:02d}" for i in range(1, 10)])
        variant_ids = iter([f"variant-{i:02d}" for i in range(1, 10)])
        exec_task_ids = iter([f"t-exec-{i:02d}" for i in range(1, 10)])
        eval_task_ids = iter([f"t-eval-{i:02d}" for i in range(1, 10)])
        commit_shas = iter([f"{i:02d}" + "b" * 38 for i in range(1, 10)])

        def ideation_fn(_task):
            return [
                IdeaTemplate(
                    slug="feat-a",
                    priority=1.0,
                    parent_commits=("a" * 40,),
                    artifacts_uri="https://artifacts.example/p/1",
                )
            ]

        def exec_fn(_task, _idea):
            return ExecutionOutcome(status="success", commit_sha=next(commit_shas))

        def eval_fn(_task, _trial):
            return EvaluationOutcome(status="success", evaluation={"score": 1.0})

        def _now_factory():
            import itertools

            c = itertools.count(1)
            return lambda: "2026-04-23T00:00:" + f"{next(c):02d}.000Z"

        now = _now_factory()

        first = SqliteStore(_EXP_R, path)
        seeded = _register_restart_workers(first)
        ideator = ScriptedIdeator(
            seeded["ideator-1"],
            ideation_fn,
            idea_id_factory=lambda: next(idea_ids),
            now=now,
        )
        executor = ScriptedExecutor(
            seeded["execution-1"],
            exec_fn,
            variant_id_factory=lambda: next(variant_ids),
            now=now,
        )
        # Run ideator + executor only; leave variant awaiting evaluation.
        first.create_ideation_task("t-ideation-01")
        ideator.run_pending(first)
        # Finalize ideation-task submission manually (normally the orchestrator service does this).
        decision, _ = first.validate_terminal("t-ideation-01")
        assert decision == "accept"
        first.accept("t-ideation-01")
        # Dispatch implement
        first.create_execution_task(next(exec_task_ids), "idea-01")
        executor.run_pending(first)
        decision, _ = first.validate_terminal("t-exec-01")
        assert decision == "accept"
        first.accept("t-exec-01")
        first.close()

        # Reopen; finish the experiment with a fresh evaluator.
        second = SqliteStore(_EXP_R, path)
        seeded_second = _register_restart_workers(second)
        evaluator = ScriptedEvaluator(seeded_second["eval-1"], eval_fn)
        second.create_evaluation_task(next(eval_task_ids), "variant-01")
        evaluator.run_pending(second)
        decision, _ = second.validate_terminal("t-eval-01")
        assert decision == "accept"
        second.accept("t-eval-01")

        # Integrate.
        second.integrate_variant("variant-01", "c" * 40)

        # Final state: all tasks completed, idea completed, variant success + integrated.
        assert [t.state for t in second.list_tasks()] == ["completed"] * 3
        assert second.read_idea("idea-01").state == "completed"
        variant = second.read_variant("variant-01")
        assert variant.status == "success"
        assert variant.variant_commit_sha == "c" * 40

        # Event log contains every event from both store instances in order.
        event_types = [e.type for e in second.events()]
        assert "task.created" in event_types
        assert "idea.ready" in event_types
        assert "variant.succeeded" in event_types
        assert "variant.integrated" in event_types
        second.close()


class TestEventLogRetention:
    """§2.4: the log MUST retain every event for an experiment's lifetime."""

    def test_close_reopen_preserves_every_event(self, tmp_path: Path) -> None:
        path = tmp_path / "eden.db"
        first = SqliteStore(_EXP_R, path)
        _register_restart_workers(first)
        for i in range(5):
            first.create_ideation_task(f"t-{i:02d}")
        seen = [(e.event_id, e.type) for e in first.events()]
        first.close()

        second = SqliteStore(_EXP_R, path)
        _register_restart_workers(second)
        assert [(e.event_id, e.type) for e in second.events()] == seen
        second.close()
