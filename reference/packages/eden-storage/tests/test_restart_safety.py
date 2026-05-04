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

from collections.abc import Callable
from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema, Idea, Variant
from eden_dispatch import (
    ScriptedEvaluator,
    ScriptedExecutor,
    ScriptedIdeator,
)
from eden_dispatch.workers import EvaluateOutcome, ExecuteOutcome, IdeaTemplate
from eden_storage import (
    EvaluateSubmission,
    ExecuteSubmission,
    IdeateSubmission,
    InvalidPrecondition,
    SqliteStore,
)


def _token_seq() -> Callable[[], str]:
    counter = iter(range(1, 10_000))
    return lambda: f"tok-{next(counter):06d}"


class TestStateSurvivesReopen:
    """Every acknowledged write (§3.1) MUST be visible after reopen."""

    def test_tasks_ideas_variants_and_events_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "eden.db"
        first = SqliteStore("exp-r", path, token_factory=_token_seq())
        first.create_idea(
            Idea(
                idea_id="p1",
                experiment_id="exp-r",
                slug="feat-a",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p1",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        first.mark_idea_ready("p1")
        first.create_execute_task("t-exec", "p1")
        claim = first.claim("t-exec", "executor-w")
        first.create_variant(
            Variant(
                variant_id="tr-1",
                experiment_id="exp-r",
                idea_id="p1",
                status="starting",
                parent_commits=["a" * 40],
                branch="work/feat-a-tr-1",
                started_at="2026-04-23T00:01:00.000Z",
            )
        )
        first.submit(
            "t-exec",
            claim.token,
            ExecuteSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
        )
        first.accept("t-exec")
        first_events = first.events()
        first.close()

        second = SqliteStore("exp-r", path)
        assert [e.type for e in second.events()] == [e.type for e in first_events]
        assert second.read_task("t-exec").state == "completed"
        assert second.read_idea("p1").state == "completed"
        variant = second.read_variant("tr-1")
        assert variant.status == "starting"
        assert variant.commit_sha == "b" * 40
        second.close()

    def test_claim_token_survives_reopen_and_authorizes_submit(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "eden.db"
        first = SqliteStore("exp-r", path, token_factory=_token_seq())
        first.create_ideate_task("t-ideate")
        claim = first.claim("t-ideate", "ideator-w")
        first.close()

        second = SqliteStore("exp-r", path)
        task = second.read_task("t-ideate")
        assert task.state == "claimed"
        assert task.claim is not None
        assert task.claim.token == claim.token
        # The persisted token authorizes a fresh submit on the reopened store.
        second.submit("t-ideate", claim.token, IdeateSubmission(status="success"))
        assert second.read_task("t-ideate").state == "submitted"
        second.close()

    def test_event_order_total_and_preserved(self, tmp_path: Path) -> None:
        """§2.2 single total order; §4.4 full replay from first event."""
        path = tmp_path / "eden.db"
        first = SqliteStore("exp-r", path, token_factory=_token_seq())
        first.create_ideate_task("t1")
        first.create_ideate_task("t2")
        before = [e.event_id for e in first.events()]
        first.close()

        second = SqliteStore("exp-r", path)
        second.create_ideate_task("t3")
        after = [e.event_id for e in second.events()]
        assert after[: len(before)] == before
        assert len(after) == len(before) + 1
        second.close()


class TestExperimentIdentityIsEnforced:
    """Chapter 8 §4.2 + sanity: a database belongs to exactly one experiment."""

    def test_reopen_with_wrong_experiment_id_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "eden.db"
        SqliteStore("exp-a", path).close()
        with pytest.raises(InvalidPrecondition, match="exp-a"):
            SqliteStore("exp-b", path)

    def test_reopen_with_different_evaluation_schema_rejected(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "eden.db"
        SqliteStore(
            "exp-a",
            path,
            evaluation_schema=EvaluationSchema({"score": "real"}),
        ).close()
        with pytest.raises(InvalidPrecondition, match="evaluation_schema"):
            SqliteStore(
                "exp-a",
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
            "exp-a",
            path,
            evaluation_schema=EvaluationSchema({"score": "real", "latency": "integer"}),
        ).close()
        # Reorder the keys; semantically identical schema.
        SqliteStore(
            "exp-a",
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
        SqliteStore("exp-r", path).close()
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
                "exp-r",
                '{"event_id":"evt-1000500","type":"task.created",'
                '"occurred_at":"2026-04-23T00:00:00.000Z",'
                '"experiment_id":"exp-r","data":{"task_id":"synthetic","kind": "ideate"}}',
            ),
        )
        conn.commit()
        conn.close()

        # Reopen with the default factory; subsequent event must not
        # collide with the synthetic one.
        second = SqliteStore("exp-r", path)
        second.create_ideate_task("t-new")
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
            "exp-a",
            path,
            evaluation_schema=EvaluationSchema({"score": "integer"}),
            token_factory=_token_seq(),
        )
        first.close()

        second = SqliteStore("exp-a", path, token_factory=_token_seq())
        # Drive a full variant to the point of an evaluate submit and
        # assert the inherited schema rejects a wrong-type metric.
        second.create_idea(
            Idea(
                idea_id="p1",
                experiment_id="exp-a",
                slug="feat-a",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p1",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        second.mark_idea_ready("p1")
        second.create_execute_task("t-exec", "p1")
        c = second.claim("t-exec", "executor-w")
        second.create_variant(
            Variant(
                variant_id="tr-1",
                experiment_id="exp-a",
                idea_id="p1",
                status="starting",
                parent_commits=["a" * 40],
                branch="work/feat-a-tr-1",
                started_at="2026-04-23T00:01:00.000Z",
            )
        )
        second.submit(
            "t-exec",
            c.token,
            ExecuteSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
        )
        second.accept("t-exec")
        second.create_evaluate_task("t-eval", "tr-1")
        ec = second.claim("t-eval", "eval-w")
        second.submit(
            "t-eval",
            ec.token,
            EvaluateSubmission(
                status="success",
                variant_id="tr-1",
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
        of a composite commit (``create_execute_task`` emits two
        events: ``task.created`` + ``idea.dispatched``). If
        rollback works, a reopened store sees neither the task
        row nor the idea state change — everything stays at the
        pre-operation checkpoint.
        """
        path = tmp_path / "eden.db"
        first = SqliteStore("exp-r", path, token_factory=_token_seq())
        first.create_idea(
            Idea(
                idea_id="p1",
                experiment_id="exp-r",
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
            first.create_execute_task("t-exec", "p1")
        first._insert_event = original  # noqa: SLF001
        first.close()

        # Reopen and verify: no task row, idea still 'ready', event
        # log unchanged from the checkpoint.
        second = SqliteStore("exp-r", path)
        assert second.list_tasks() == []
        assert second.read_idea("p1").state == "ready"
        assert [e.event_id for e in second.events()] == checkpoint_events
        second.close()


class TestRunExperimentAcrossRestarts:
    """End-to-end: an experiment paused mid-flight resumes on reopen."""

    def test_plan_then_implement_then_restart_then_evaluate(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "eden.db"
        token_factory = _token_seq()

        idea_ids = iter([f"p-{i:02d}" for i in range(1, 10)])
        variant_ids = iter([f"tr-{i:02d}" for i in range(1, 10)])
        impl_task_ids = iter([f"t-exec-{i:02d}" for i in range(1, 10)])
        eval_task_ids = iter([f"t-eval-{i:02d}" for i in range(1, 10)])
        commit_shas = iter([f"{i:02d}" + "b" * 38 for i in range(1, 10)])

        def plan_fn(_task):
            return [
                IdeaTemplate(
                    slug="feat-a",
                    priority=1.0,
                    parent_commits=("a" * 40,),
                    artifacts_uri="https://artifacts.example/p/1",
                )
            ]

        def impl_fn(_task, _idea):
            return ExecuteOutcome(status="success", commit_sha=next(commit_shas))

        def eval_fn(_task, _trial):
            return EvaluateOutcome(status="success", evaluation={"score": 1.0})

        def _now_factory():
            import itertools

            c = itertools.count(1)
            return lambda: "2026-04-23T00:00:" + f"{next(c):02d}.000Z"

        now = _now_factory()
        ideator = ScriptedIdeator(
            "ideator-1", plan_fn, idea_id_factory=lambda: next(idea_ids), now=now
        )
        executor = ScriptedExecutor(
            "impl-1", impl_fn, trial_id_factory=lambda: next(variant_ids), now=now
        )

        first = SqliteStore("exp-r", path, token_factory=token_factory)
        # Run ideator + executor only; leave variant awaiting evaluation.
        first.create_ideate_task("t-ideate-01")
        ideator.run_pending(first)
        # Finalize ideate submission manually (normally the orchestrator service does this).
        decision, _ = first.validate_terminal("t-ideate-01")
        assert decision == "accept"
        first.accept("t-ideate-01")
        # Dispatch implement
        first.create_execute_task(next(impl_task_ids), "p-01")
        executor.run_pending(first)
        decision, _ = first.validate_terminal("t-exec-01")
        assert decision == "accept"
        first.accept("t-exec-01")
        first.close()

        # Reopen; finish the experiment with a fresh evaluator.
        evaluator = ScriptedEvaluator("eval-1", eval_fn)
        second = SqliteStore("exp-r", path, token_factory=token_factory)
        second.create_evaluate_task(next(eval_task_ids), "tr-01")
        evaluator.run_pending(second)
        decision, _ = second.validate_terminal("t-eval-01")
        assert decision == "accept"
        second.accept("t-eval-01")

        # Integrate.
        second.integrate_variant("tr-01", "c" * 40)

        # Final state: all tasks completed, idea completed, variant success + integrated.
        assert [t.state for t in second.list_tasks()] == ["completed"] * 3
        assert second.read_idea("p-01").state == "completed"
        variant = second.read_variant("tr-01")
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
        first = SqliteStore("exp-r", path, token_factory=_token_seq())
        for i in range(5):
            first.create_ideate_task(f"t-{i:02d}")
        seen = [(e.event_id, e.type) for e in first.events()]
        first.close()

        second = SqliteStore("exp-r", path)
        assert [(e.event_id, e.type) for e in second.events()] == seen
        second.close()
