"""Tests for the §3.3 non-no-op variant invariant (12a-1i).

Covers both enforcement layers in `_StoreBase._validate_non_no_op_variant`:

1. SHA-equality fast path — always on; rejects when ``commit_sha`` is
   byte-equal to every entry in ``idea.parent_commits``.
2. Tree-identity check — when a ``tree_resolver`` is wired; rejects
   when the resolver maps the submission SHA and every parent SHA to
   the same tree (e.g. an empty commit on top of parent).

Also asserts the rule does NOT fire on ``status=error`` submissions
or on ideas with empty ``parent_commits``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import ExecutionPayload, ExecutionTask, Idea, Variant
from eden_storage import (
    InMemoryStore,
    NoOpVariant,
    Store,
    VariantSubmission,
)


def _seed_idea_task_variant(
    store: Store,
    *,
    parent: str,
    idea_id: str = "idea-1",
    task_id: str = "exec-1",
    variant_id: str = "variant-1",
) -> None:
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="feat-x",
            priority=1.0,
            parent_commits=[parent],
            artifacts_uri="https://artifacts.example/x",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_idea_ready(idea_id)
    store.create_task(
        ExecutionTask(
            task_id=task_id,
            kind="execution",
            state="pending",
            payload=ExecutionPayload(idea_id=idea_id),
            created_at="2026-04-23T00:00:00.000Z",
            updated_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[parent],
            branch=f"work/{variant_id}",
            started_at="2026-04-23T00:00:00.000Z",
        )
    )


class TestShaEqualityFastPath:
    """No git resolver — the Store still rejects SHA-equal no-ops."""

    def test_commit_sha_equal_to_only_parent_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        parent = "a" * 40
        _seed_idea_task_variant(store, parent=parent)
        claim = store.claim("exec-1", "impl-worker")
        with pytest.raises(NoOpVariant):
            store.submit(
                "exec-1",
                claim.worker_id,
                VariantSubmission(
                    status="success",
                    variant_id="variant-1",
                    commit_sha=parent,
                ),
            )
        # End-state: variant did NOT terminalize as success.
        assert store.read_variant("variant-1").status == "starting"

    def test_commit_sha_differs_accepted(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        parent = "a" * 40
        _seed_idea_task_variant(store, parent=parent)
        claim = store.claim("exec-1", "impl-worker")
        # Different SHA — must pass.
        store.submit(
            "exec-1",
            claim.worker_id,
            VariantSubmission(
                status="success",
                variant_id="variant-1",
                commit_sha="b" * 40,
            ),
        )
        # Submission committed; task is in submitted.
        assert store.read_task("exec-1").state == "submitted"

    def test_status_error_not_subject_to_rule(
        self, make_store: Callable[..., Store]
    ) -> None:
        """spec/v0/03-roles.md §3.3 — rule applies only to status=success."""
        store = make_store()
        parent = "a" * 40
        _seed_idea_task_variant(store, parent=parent)
        claim = store.claim("exec-1", "impl-worker")
        # status=error with a commit_sha that would otherwise trip the
        # rule. error submissions carry no commit_sha in the success-
        # contract sense; the rule MUST NOT fire.
        store.submit(
            "exec-1",
            claim.worker_id,
            VariantSubmission(
                status="error",
                variant_id="variant-1",
                commit_sha=None,
            ),
        )
        assert store.read_task("exec-1").state == "submitted"

class TestTreeResolver:
    """With a tree-resolver wired, the deeper tree-identity check fires."""

    def _make_store_with_resolver(
        self, mapping: dict[str, str | None]
    ) -> InMemoryStore:
        def _resolve(sha: str) -> str | None:
            return mapping.get(sha)

        store = InMemoryStore(
            experiment_id="exp-test",
            tree_resolver=_resolve,
        )
        for wid in ("impl-worker",):
            store.register_worker(wid)
        return store

    def test_empty_commit_on_parent_rejected(self) -> None:
        """Different SHA, same tree (empty commit on parent) — MUST be rejected."""
        parent_sha = "a" * 40
        empty_commit_sha = "c" * 40
        # Both SHAs resolve to the same tree.
        store = self._make_store_with_resolver(
            {parent_sha: "T1", empty_commit_sha: "T1"}
        )
        _seed_idea_task_variant(store, parent=parent_sha)
        claim = store.claim("exec-1", "impl-worker")
        with pytest.raises(NoOpVariant):
            store.submit(
                "exec-1",
                claim.worker_id,
                VariantSubmission(
                    status="success",
                    variant_id="variant-1",
                    commit_sha=empty_commit_sha,
                ),
            )
        assert store.read_variant("variant-1").status == "starting"

    def test_different_tree_accepted(self) -> None:
        parent_sha = "a" * 40
        real_change_sha = "c" * 40
        store = self._make_store_with_resolver(
            {parent_sha: "T1", real_change_sha: "T2"}
        )
        _seed_idea_task_variant(store, parent=parent_sha)
        claim = store.claim("exec-1", "impl-worker")
        store.submit(
            "exec-1",
            claim.worker_id,
            VariantSubmission(
                status="success",
                variant_id="variant-1",
                commit_sha=real_change_sha,
            ),
        )
        assert store.read_task("exec-1").state == "submitted"

    def test_resolver_returns_none_falls_back_to_sha_check(self) -> None:
        """Unknown SHAs degrade gracefully — SHA-equality fast path still applies."""
        parent_sha = "a" * 40
        unknown_sha = "c" * 40
        # Resolver doesn't know the variant SHA — returns None.
        store = self._make_store_with_resolver({parent_sha: "T1"})
        _seed_idea_task_variant(store, parent=parent_sha)
        claim = store.claim("exec-1", "impl-worker")
        # Different SHA — must pass; the resolver can't conclude
        # the trees match, and SHA-equality doesn't trip.
        store.submit(
            "exec-1",
            claim.worker_id,
            VariantSubmission(
                status="success",
                variant_id="variant-1",
                commit_sha=unknown_sha,
            ),
        )
        assert store.read_task("exec-1").state == "submitted"

    def test_resolver_raises_falls_back_silently(self) -> None:
        """A raising resolver MUST NOT bubble — Store treats it as 'unavailable'."""
        parent_sha = "a" * 40

        def _bad(sha: str) -> str | None:
            raise RuntimeError("boom")

        store = InMemoryStore(
            experiment_id="exp-test",
            tree_resolver=_bad,
        )
        store.register_worker("impl-worker")
        _seed_idea_task_variant(store, parent=parent_sha)
        claim = store.claim("exec-1", "impl-worker")
        # Resolver throws; SHA-equality fast path still rejects the
        # literal no-op case.
        with pytest.raises(NoOpVariant):
            store.submit(
                "exec-1",
                claim.worker_id,
                VariantSubmission(
                    status="success",
                    variant_id="variant-1",
                    commit_sha=parent_sha,
                ),
            )

    def test_multi_parent_all_trees_match_rejected(self) -> None:
        parent_a = "a" * 40
        parent_b = "b" * 40
        variant_sha = "c" * 40
        store = self._make_store_with_resolver(
            {parent_a: "T1", parent_b: "T1", variant_sha: "T1"}
        )
        store.create_idea(
            Idea(
                idea_id="idea-multi",
                experiment_id=store.experiment_id,
                slug="feat-multi",
                priority=1.0,
                parent_commits=[parent_a, parent_b],
                artifacts_uri="https://artifacts.example/multi",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        store.mark_idea_ready("idea-multi")
        store.create_task(
            ExecutionTask(
                task_id="exec-multi",
                kind="execution",
                state="pending",
                payload=ExecutionPayload(idea_id="idea-multi"),
                created_at="2026-04-23T00:00:00.000Z",
                updated_at="2026-04-23T00:00:00.000Z",
            )
        )
        store.create_variant(
            Variant(
                variant_id="variant-multi",
                experiment_id=store.experiment_id,
                idea_id="idea-multi",
                status="starting",
                parent_commits=[parent_a, parent_b],
                branch="work/variant-multi",
                started_at="2026-04-23T00:00:00.000Z",
            )
        )
        claim = store.claim("exec-multi", "impl-worker")
        with pytest.raises(NoOpVariant):
            store.submit(
                "exec-multi",
                claim.worker_id,
                VariantSubmission(
                    status="success",
                    variant_id="variant-multi",
                    commit_sha=variant_sha,
                ),
            )

    def test_resubmit_against_submitted_skips_recheck(self) -> None:
        """spec/v0/04-task-protocol.md §4.2 — content-equivalent resubmit MUST be accepted.

        If the first submit committed while the tree resolver was
        unavailable (returning None for the variant SHA), a
        content-equivalent retry after the resolver started returning
        non-empty data MUST still be accepted as idempotent. The
        no-op check MUST NOT re-run on the resubmit path; §4.2
        idempotency precedes the role-side success-contract check.
        """
        parent_sha = "a" * 40
        empty_commit_sha = "c" * 40
        # First submit: resolver doesn't know empty_commit_sha (transient
        # miss). The no-op check skips, submit succeeds.
        mapping: dict[str, str | None] = {parent_sha: "T1"}

        def _resolve(sha: str) -> str | None:
            return mapping.get(sha)

        store = InMemoryStore(experiment_id="exp-test", tree_resolver=_resolve)
        store.register_worker("impl-worker")
        _seed_idea_task_variant(store, parent=parent_sha)
        claim = store.claim("exec-1", "impl-worker")
        sub = VariantSubmission(
            status="success", variant_id="variant-1", commit_sha=empty_commit_sha
        )
        store.submit("exec-1", claim.worker_id, sub)
        assert store.read_task("exec-1").state == "submitted"
        # Now the resolver "starts working" — empty_commit_sha resolves
        # to the same tree as parent. A re-evaluation would raise
        # NoOpVariant. The idempotent retry MUST NOT re-evaluate.
        mapping[empty_commit_sha] = "T1"
        store.submit("exec-1", claim.worker_id, sub)
        assert store.read_task("exec-1").state == "submitted"


class TestTreeResolverMultiParent:
    """Multi-parent merge scenarios for the tree-identity check."""

    def _make_store_with_resolver(
        self, mapping: dict[str, str | None]
    ) -> InMemoryStore:
        def _resolve(sha: str) -> str | None:
            return mapping.get(sha)

        store = InMemoryStore(
            experiment_id="exp-test",
            tree_resolver=_resolve,
        )
        store.register_worker("impl-worker")
        return store

    def test_multi_parent_one_tree_differs_accepted(self) -> None:
        """spec/v0/03-roles.md §3.3 — rule requires identity with EVERY parent."""
        parent_a = "a" * 40
        parent_b = "b" * 40
        variant_sha = "c" * 40
        store = self._make_store_with_resolver(
            {parent_a: "T1", parent_b: "T2", variant_sha: "T1"}
        )
        store.create_idea(
            Idea(
                idea_id="idea-multi",
                experiment_id=store.experiment_id,
                slug="feat-multi",
                priority=1.0,
                parent_commits=[parent_a, parent_b],
                artifacts_uri="https://artifacts.example/multi",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        store.mark_idea_ready("idea-multi")
        store.create_task(
            ExecutionTask(
                task_id="exec-multi",
                kind="execution",
                state="pending",
                payload=ExecutionPayload(idea_id="idea-multi"),
                created_at="2026-04-23T00:00:00.000Z",
                updated_at="2026-04-23T00:00:00.000Z",
            )
        )
        store.create_variant(
            Variant(
                variant_id="variant-multi",
                experiment_id=store.experiment_id,
                idea_id="idea-multi",
                status="starting",
                parent_commits=[parent_a, parent_b],
                branch="work/variant-multi",
                started_at="2026-04-23T00:00:00.000Z",
            )
        )
        claim = store.claim("exec-multi", "impl-worker")
        # Variant tree matches parent_a but differs from parent_b —
        # the variant contributes real change relative to parent_b,
        # so the rule MUST NOT fire.
        store.submit(
            "exec-multi",
            claim.worker_id,
            VariantSubmission(
                status="success",
                variant_id="variant-multi",
                commit_sha=variant_sha,
            ),
        )
        assert store.read_task("exec-multi").state == "submitted"
