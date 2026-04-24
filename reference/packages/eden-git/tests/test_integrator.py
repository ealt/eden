"""Integrator tests (spec/v0/06-integrator.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from eden_contracts import MetricsSchema, Proposal, Trial
from eden_git import (
    AtomicityViolation,
    CorruptIntegrationState,
    EvalManifestPathCollision,
    GitRepo,
    Identity,
    Integrator,
    NotReadyForIntegration,
    ReachabilityViolation,
    TreeEntry,
)
from eden_git._manifest import build_manifest
from eden_storage import InMemoryStore, Store

AUTHOR = Identity("Integrator", "integrator@eden.example")
FIXED_CLOCK = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    """Non-bare git repo with an initial commit on ``main``."""
    r = GitRepo.init(tmp_path / "repo")
    # Seed an initial commit so `parent_commits` has something to point at.
    blob = r.write_blob(b"initial\n")
    tree = r.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
    )
    seed = r.commit_tree(
        tree,
        parents=[],
        message="initial\n",
        author=AUTHOR,
        author_date="2026-04-23T00:00:00+00:00",
        committer_date="2026-04-23T00:00:00+00:00",
    )
    r.update_ref("refs/heads/main", seed)
    return r


@pytest.fixture
def seed_sha(repo: GitRepo) -> str:
    result = repo.resolve_ref("refs/heads/main")
    assert result is not None
    return result


@pytest.fixture
def store() -> Store:
    return InMemoryStore(experiment_id="exp-1")


@pytest.fixture
def integrator(store: Store, repo: GitRepo) -> Integrator:
    return Integrator(
        store=store,
        repo=repo,
        author=AUTHOR,
        clock=lambda: FIXED_CLOCK,
    )


# ----------------------------------------------------------------------
# Harness helpers
# ----------------------------------------------------------------------


def _make_proposal(store: Store, *, proposal_id: str, slug: str, parent: str) -> None:
    store.create_proposal(
        Proposal(
            proposal_id=proposal_id,
            experiment_id=store.experiment_id,
            slug=slug,
            priority=1.0,
            parent_commits=[parent],
            artifacts_uri="file:///tmp/artifacts/",
            state="drafting",
            created_at="2026-04-23T00:00:00Z",
        )
    )


def _make_work_branch(
    repo: GitRepo,
    *,
    branch: str,
    parent: str,
    extra_file: tuple[str, bytes] = ("work.py", b"print('hi')\n"),
) -> str:
    """Create a ``work/*`` branch with one commit on top of ``parent``. Returns the commit SHA."""
    blob = repo.write_blob(extra_file[1])
    base_tree = repo.commit_tree_sha(parent)
    new_tree = repo.write_tree_with_file(base_tree, extra_file[0], blob)
    commit = repo.commit_tree(
        new_tree,
        parents=[parent],
        message=f"work on {branch}\n",
        author=AUTHOR,
        author_date="2026-04-23T01:00:00+00:00",
        committer_date="2026-04-23T01:00:00+00:00",
    )
    repo.update_ref(f"refs/heads/{branch}", commit)
    return commit


def _seed_success_trial(
    store: Store,
    *,
    trial_id: str,
    proposal_id: str,
    slug: str,
    parent: str,
    branch: str,
    commit_sha: str,
    metrics: dict[str, Any] | None = None,
) -> Trial:
    """Create proposal (if absent) and starting/success trial with the given work-branch tip."""
    if not list(store.list_proposals()) or all(
        p.proposal_id != proposal_id for p in store.list_proposals()
    ):
        _make_proposal(store, proposal_id=proposal_id, slug=slug, parent=parent)
    trial = Trial(
        trial_id=trial_id,
        experiment_id=store.experiment_id,
        proposal_id=proposal_id,
        status="starting",
        parent_commits=[parent],
        branch=branch,
        started_at="2026-04-23T00:30:00Z",
    )
    store.create_trial(trial)
    # Transition to success by raw state patch: the full orchestrator
    # path is not needed here — these tests exercise the integrator in
    # isolation. The store's public API goes via submit/accept; that
    # would require a full dispatch harness. Tests that need end-to-end
    # integration with the dispatch loop live in the eden-dispatch
    # test suite.
    return _force_trial_success(
        store,
        trial_id,
        commit_sha=commit_sha,
        metrics=metrics if metrics is not None else {"score": 1.0},
    )


def _force_trial_success(
    store: Store,
    trial_id: str,
    *,
    commit_sha: str,
    metrics: dict[str, Any],
) -> Trial:
    """Backdoor: set a trial to ``success`` with the given commit/metrics.

    Uses the store's internal ``_trials`` / ``_events`` mutables rather
    than the public submit/accept path, so these tests don't depend on
    the full dispatch harness. This is a test-only shortcut; production
    code MUST go via ``submit`` + ``accept``.
    """
    from eden_storage._base import _validated_update

    inner = store
    trial = inner.read_trial(trial_id)
    updated = _validated_update(
        trial,
        status="success",
        commit_sha=commit_sha,
        metrics=metrics,
        completed_at="2026-04-23T02:00:00Z",
    )
    inner._trials[trial_id] = updated  # type: ignore[attr-defined]
    return updated


# ----------------------------------------------------------------------
# Success path
# ----------------------------------------------------------------------


class TestSuccessPath:
    def test_integrate_writes_ref_field_and_event(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        trial = _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )

        result = integrator.integrate("tr-1")

        assert result.already_integrated is False
        assert result.branch == "trial/tr-1-speedup"
        # Ref exists at the returned SHA.
        ref_sha = repo.resolve_ref("refs/heads/trial/tr-1-speedup")
        assert ref_sha == result.trial_commit_sha
        # Field + event in store.
        fresh = store.read_trial("tr-1")
        assert fresh.trial_commit_sha == result.trial_commit_sha
        integrated = [e for e in store.events() if e.type == "trial.integrated"]
        assert len(integrated) == 1
        assert integrated[0].data["trial_id"] == "tr-1"
        assert integrated[0].data["trial_commit_sha"] == result.trial_commit_sha
        # Parents of the trial commit match trial.parent_commits.
        assert repo.commit_parents(result.trial_commit_sha) == list(
            trial.parent_commits
        )

    def test_commit_subject_matches_spec_3_3(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        result = integrator.integrate("tr-1")
        subject = repo.commit_message_subject(result.trial_commit_sha)
        assert subject == "trial: tr-1 speedup"

    def test_squash_tree_equals_worker_tip_plus_manifest(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        trial = _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        result = integrator.integrate("tr-1")

        worker_tree = repo.commit_tree_sha(commit)
        squash_tree = repo.commit_tree_sha(result.trial_commit_sha)

        worker_entries = {
            (e.mode, e.type, e.path): e.sha
            for e in repo.ls_tree(worker_tree, recursive=True)
        }
        squash_entries = {
            (e.mode, e.type, e.path): e.sha
            for e in repo.ls_tree(squash_tree, recursive=True)
        }

        # Every worker-tip entry is preserved unchanged.
        for key, sha in worker_entries.items():
            assert squash_entries.get(key) == sha

        # Exactly one extra entry: the eval manifest.
        extras = {k: v for k, v in squash_entries.items() if k not in worker_entries}
        assert list(extras) == [("100644", "blob", ".eden/trials/tr-1/eval.json")]

        # Manifest blob matches build_manifest(trial).
        manifest_sha = extras[("100644", "blob", ".eden/trials/tr-1/eval.json")]
        assert repo.read_blob(manifest_sha) == build_manifest(
            store.read_trial(trial.trial_id)
        )

    def test_multi_parent_trial_produces_multi_parent_commit(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        # Build a second parent reachable from work tip.
        second_blob = repo.write_blob(b"side\n")
        second_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=second_blob, path="side.txt")]
        )
        second_parent = repo.commit_tree(
            second_tree,
            parents=[seed_sha],
            message="second parent\n",
            author=AUTHOR,
            author_date="2026-04-23T00:10:00+00:00",
            committer_date="2026-04-23T00:10:00+00:00",
        )
        # Worker commit is a merge of seed + second_parent.
        merge_tree = repo.write_tree_with_file(
            repo.commit_tree_sha(seed_sha), "merged.py", repo.write_blob(b"m\n")
        )
        merge_commit = repo.commit_tree(
            merge_tree,
            parents=[seed_sha, second_parent],
            message="merge\n",
            author=AUTHOR,
            author_date="2026-04-23T01:00:00+00:00",
            committer_date="2026-04-23T01:00:00+00:00",
        )
        repo.update_ref("refs/heads/work/tr-m", merge_commit)

        _make_proposal(store, proposal_id="p-m", slug="merge", parent=seed_sha)
        store.create_trial(
            Trial(
                trial_id="tr-m",
                experiment_id=store.experiment_id,
                proposal_id="p-m",
                status="starting",
                parent_commits=[seed_sha, second_parent],
                branch="work/tr-m",
                started_at="2026-04-23T00:30:00Z",
            )
        )
        _force_trial_success(
            store, "tr-m", commit_sha=merge_commit, metrics={"score": 1.0}
        )

        result = integrator.integrate("tr-m")
        assert repo.commit_parents(result.trial_commit_sha) == [seed_sha, second_parent]

    def test_branch_tip_descending_past_commit_sha_still_integrates(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        """§2: commit_sha reachable from branch tip is sufficient."""
        work_commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        # Advance the branch with an extra commit after evaluation.
        extra_blob = repo.write_blob(b"extra\n")
        extra_tree = repo.write_tree_with_file(
            repo.commit_tree_sha(work_commit), "extra.py", extra_blob
        )
        extra_commit = repo.commit_tree(
            extra_tree,
            parents=[work_commit],
            message="post-eval\n",
            author=AUTHOR,
            author_date="2026-04-23T02:00:00+00:00",
            committer_date="2026-04-23T02:00:00+00:00",
        )
        repo.update_ref("refs/heads/work/tr-1", extra_commit)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=work_commit,  # evaluated at the earlier commit
        )
        result = integrator.integrate("tr-1")
        # Squash tree derives from work_commit, NOT from the advanced tip.
        squash_tree = repo.commit_tree_sha(result.trial_commit_sha)
        squash_paths = {e.path for e in repo.ls_tree(squash_tree, recursive=True)}
        # work.py is in; extra.py is not (it's after commit_sha).
        assert "work.py" in squash_paths
        assert "extra.py" not in squash_paths


# ----------------------------------------------------------------------
# Idempotency (§5.3)
# ----------------------------------------------------------------------


class TestIdempotency:
    def test_second_integrate_is_noop(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        first = integrator.integrate("tr-1")
        events_before = len(store.events())

        second = integrator.integrate("tr-1")

        assert second.already_integrated is True
        assert second.trial_commit_sha == first.trial_commit_sha
        assert len(store.events()) == events_before

    def test_corrupt_state_trial_commit_sha_without_ref(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        # Integrate, then delete the ref externally.
        result = integrator.integrate("tr-1")
        repo.delete_ref("refs/heads/trial/tr-1-speedup")
        # Trial still has trial_commit_sha set.
        assert store.read_trial("tr-1").trial_commit_sha == result.trial_commit_sha

        with pytest.raises(CorruptIntegrationState):
            integrator.integrate("tr-1")


# ----------------------------------------------------------------------
# Reachability (§1.4)
# ----------------------------------------------------------------------


class TestReachability:
    def test_commit_sha_not_descending_from_parents_rejected(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        # Build a parallel commit chain that doesn't descend from seed_sha.
        other_blob = repo.write_blob(b"parallel\n")
        other_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=other_blob, path="p.py")]
        )
        other_root = repo.commit_tree(
            other_tree,
            parents=[],
            message="parallel root\n",
            author=AUTHOR,
            author_date="2026-04-23T00:00:00+00:00",
            committer_date="2026-04-23T00:00:00+00:00",
        )
        repo.update_ref("refs/heads/work/tr-1", other_root)

        # Trial declares seed_sha as parent but commit_sha is other_root.
        _make_proposal(store, proposal_id="p-1", slug="speedup", parent=seed_sha)
        store.create_trial(
            Trial(
                trial_id="tr-1",
                experiment_id=store.experiment_id,
                proposal_id="p-1",
                status="starting",
                parent_commits=[seed_sha],
                branch="work/tr-1",
                started_at="2026-04-23T00:30:00Z",
            )
        )
        _force_trial_success(
            store, "tr-1", commit_sha=other_root, metrics={"score": 1.0}
        )

        with pytest.raises(ReachabilityViolation):
            integrator.integrate("tr-1")

        _assert_no_side_effects(store, repo, branch="trial/tr-1-speedup")


# ----------------------------------------------------------------------
# Manifest-path collision (§3.2)
# ----------------------------------------------------------------------


class TestManifestPathCollision:
    def test_worker_already_has_manifest_path_rejected(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        # Build a work branch whose tree already has the manifest file.
        base_tree = repo.commit_tree_sha(seed_sha)
        collision_blob = repo.write_blob(b'{"rogue":true}\n')
        new_tree = repo.write_tree_with_file(
            base_tree, ".eden/trials/tr-1/eval.json", collision_blob
        )
        commit = repo.commit_tree(
            new_tree,
            parents=[seed_sha],
            message="rogue\n",
            author=AUTHOR,
            author_date="2026-04-23T01:00:00+00:00",
            committer_date="2026-04-23T01:00:00+00:00",
        )
        repo.update_ref("refs/heads/work/tr-1", commit)

        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )

        with pytest.raises(EvalManifestPathCollision):
            integrator.integrate("tr-1")

        _assert_no_side_effects(store, repo, branch="trial/tr-1-speedup")


# ----------------------------------------------------------------------
# Promotion preconditions (§2)
# ----------------------------------------------------------------------


class TestPromotionPreconditions:
    @pytest.mark.parametrize("status", ["starting", "error", "eval_error"])
    def test_non_success_status_rejected(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
        status: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        # Overwrite to the non-success status.
        from eden_storage._base import _validated_update

        trial = store.read_trial("tr-1")
        store._trials["tr-1"] = _validated_update(trial, status=status)  # type: ignore[attr-defined]

        with pytest.raises(NotReadyForIntegration):
            integrator.integrate("tr-1")
        _assert_no_side_effects(store, repo, branch="trial/tr-1-speedup")

    def test_missing_branch_rejected(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        # Delete the work branch.
        repo.delete_ref("refs/heads/work/tr-1")
        with pytest.raises(NotReadyForIntegration):
            integrator.integrate("tr-1")

    def test_branch_unrelated_to_commit_sha_rejected(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        work_commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=work_commit,
        )
        # Repoint the branch to seed (commit_sha is now a descendant of
        # branch tip — not an ancestor).
        repo.update_ref("refs/heads/work/tr-1", seed_sha, expected_old_sha=work_commit)
        with pytest.raises(NotReadyForIntegration):
            integrator.integrate("tr-1")


class TestMetricsRevalidation:
    def test_invalid_metrics_at_promotion_rejected(
        self,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        store: Store = InMemoryStore(
            experiment_id="exp-1",
            metrics_schema=MetricsSchema({"score": "integer"}),
        )
        integrator = Integrator(
            store=store, repo=repo, author=AUTHOR, clock=lambda: FIXED_CLOCK
        )
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
            metrics={"score": "not-an-int"},
        )
        with pytest.raises(NotReadyForIntegration):
            integrator.integrate("tr-1")
        _assert_no_side_effects(store, repo, branch="trial/tr-1-speedup")

    def test_no_schema_skips_validation(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
            metrics={"whatever": "goes"},  # would fail a schema, none present
        )
        result = integrator.integrate("tr-1")
        assert result.already_integrated is False


# ----------------------------------------------------------------------
# Atomic rollback (§3.4)
# ----------------------------------------------------------------------


class TestAtomicRollback:
    def test_store_failure_rolls_back_ref(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )

        boom = RuntimeError("store disk full")

        def _explode(*_a: Any, **_kw: Any) -> None:
            raise boom

        monkeypatch.setattr(store, "integrate_trial", _explode)

        with pytest.raises(RuntimeError) as exc_info:
            integrator.integrate("tr-1")
        assert exc_info.value is boom
        # Ref compensated away.
        assert repo.resolve_ref("refs/heads/trial/tr-1-speedup") is None
        # Field still absent.
        assert store.read_trial("tr-1").trial_commit_sha is None
        # No trial.integrated event.
        assert [e for e in store.events() if e.type == "trial.integrated"] == []

    def test_compensating_delete_failure_surfaces_atomicity_violation(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )

        store_boom = RuntimeError("store boom")
        rollback_boom = RuntimeError("rollback boom")

        def _store_explode(*_a: Any, **_kw: Any) -> None:
            raise store_boom

        def _ref_explode(*_a: Any, **_kw: Any) -> None:
            raise rollback_boom

        monkeypatch.setattr(store, "integrate_trial", _store_explode)
        monkeypatch.setattr(repo, "delete_ref", _ref_explode)

        with pytest.raises(AtomicityViolation) as exc_info:
            integrator.integrate("tr-1")
        assert exc_info.value.original is store_boom
        assert exc_info.value.rollback is rollback_boom


# ----------------------------------------------------------------------
# Commit identity
# ----------------------------------------------------------------------


class TestCommitIdentity:
    def test_committer_defaults_to_author(
        self,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        integ = Integrator(
            store=store, repo=repo, author=AUTHOR, clock=lambda: FIXED_CLOCK
        )
        result = integ.integrate("tr-1")
        header = repo.commit_message(result.trial_commit_sha)  # full message
        # commit_tree stamps via GIT_AUTHOR_* / GIT_COMMITTER_* env vars;
        # asserting identity requires cat-file -p.
        raw = _raw_commit(repo, result.trial_commit_sha)
        assert f"author {AUTHOR.name} <{AUTHOR.email}>" in raw
        assert f"committer {AUTHOR.name} <{AUTHOR.email}>" in raw
        assert "trial: tr-1 speedup" in header

    def test_explicit_committer_used(
        self,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        committer = Identity("CI Bot", "ci@eden.example")
        integ = Integrator(
            store=store,
            repo=repo,
            author=AUTHOR,
            committer=committer,
            clock=lambda: FIXED_CLOCK,
        )
        result = integ.integrate("tr-1")
        raw = _raw_commit(repo, result.trial_commit_sha)
        assert f"author {AUTHOR.name} <{AUTHOR.email}>" in raw
        assert f"committer {committer.name} <{committer.email}>" in raw


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _assert_no_side_effects(store: Store, repo: GitRepo, *, branch: str) -> None:
    assert repo.resolve_ref(f"refs/heads/{branch}") is None
    trials = store.list_trials()
    for trial in trials:
        assert trial.trial_commit_sha is None
    assert [e for e in store.events() if e.type == "trial.integrated"] == []


def _raw_commit(repo: GitRepo, sha: str) -> str:
    """Read the raw commit object via cat-file -p for identity assertions."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo.path), "cat-file", "-p", sha],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout


# ----------------------------------------------------------------------
# §5.3 after §1.3 work-branch cleanup
# ----------------------------------------------------------------------


class TestReplayAfterWorkerPruned:
    """§1.3 permits deleting work/* after promotion. Once the worker
    commit is pruned, §5.3 replay cannot re-verify the §3.2 tree
    shape, and the integrator must surface this rather than silently
    no-op-ing — otherwise a corrupted trial/* commit with a matching
    manifest blob but divergent tree would be silently accepted."""

    def test_replay_after_worker_gc_raises_corrupt_state(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        integrator.integrate("tr-1")

        # §1.3: delete work/* and `git gc --prune=now`.
        repo.delete_ref("refs/heads/work/tr-1")
        _force_gc_prune_now(repo)
        assert repo.commit_exists(commit) is False, (
            "test precondition: git gc should have pruned the worker commit"
        )

        with pytest.raises(CorruptIntegrationState):
            integrator.integrate("tr-1")

    def test_replay_with_worker_alive_still_noops(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        """§5.3: when the worker tree is still reachable, replay IS a no-op."""
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        first = integrator.integrate("tr-1")
        second = integrator.integrate("tr-1")
        assert second.already_integrated is True
        assert second.trial_commit_sha == first.trial_commit_sha


class TestCorruptTrialTreeRejected:
    """Codex round-1 reproduction: a trial/* commit with an extra file
    in its tree but the same eval.json bytes must be rejected on
    replay, not silently accepted."""

    def test_tree_with_extra_path_rejected(
        self,
        integrator: Integrator,
        store: Store,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
        )
        first = integrator.integrate("tr-1")

        # Rewrite the trial/* commit externally to have an extra path
        # alongside the manifest — exactly the corruption Codex flagged.
        squash_tree = repo.commit_tree_sha(first.trial_commit_sha)
        extra_blob = repo.write_blob(b"extra\n")
        corrupt_tree = repo.write_tree_with_file(squash_tree, "rogue.txt", extra_blob)
        corrupt_commit = repo.commit_tree(
            corrupt_tree,
            parents=list(store.read_trial("tr-1").parent_commits),
            message="trial: tr-1 speedup\n",
            author=AUTHOR,
            author_date="2026-04-23T03:00:00+00:00",
            committer_date="2026-04-23T03:00:00+00:00",
        )
        repo.update_ref(
            "refs/heads/trial/tr-1-speedup",
            corrupt_commit,
            expected_old_sha=first.trial_commit_sha,
        )
        # Repoint trial.trial_commit_sha to match (simulating externally-
        # consistent corruption).
        from eden_storage._base import _validated_update

        trial = store.read_trial("tr-1")
        store._trials["tr-1"] = _validated_update(  # type: ignore[attr-defined]
            trial, trial_commit_sha=corrupt_commit
        )

        with pytest.raises(CorruptIntegrationState):
            integrator.integrate("tr-1")


def _force_gc_prune_now(repo: GitRepo) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo.path), "gc", "--prune=now", "--quiet"],
        check=True,
        capture_output=True,
    )


# ----------------------------------------------------------------------
# Non-finite metrics
# ----------------------------------------------------------------------


class TestNonFiniteMetricsRejected:
    def test_nan_metric_rejected_at_integration(
        self,
        repo: GitRepo,
        seed_sha: str,
    ) -> None:
        store: Store = InMemoryStore(
            experiment_id="exp-nan",
            metrics_schema=MetricsSchema({"score": "real"}),
        )
        integ = Integrator(
            store=store, repo=repo, author=AUTHOR, clock=lambda: FIXED_CLOCK
        )
        commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
        _seed_success_trial(
            store,
            trial_id="tr-1",
            proposal_id="p-1",
            slug="speedup",
            parent=seed_sha,
            branch="work/tr-1",
            commit_sha=commit,
            metrics={"score": float("nan")},
        )
        with pytest.raises(NotReadyForIntegration):
            integ.integrate("tr-1")
        _assert_no_side_effects(store, repo, branch="trial/tr-1-speedup")


# ----------------------------------------------------------------------
# Public parity: manifest ends with newline, is valid JSON
# ----------------------------------------------------------------------


def test_manifest_committed_blob_is_parseable_json(
    integrator: Integrator,
    store: Store,
    repo: GitRepo,
    seed_sha: str,
) -> None:
    commit = _make_work_branch(repo, branch="work/tr-1", parent=seed_sha)
    _seed_success_trial(
        store,
        trial_id="tr-1",
        proposal_id="p-1",
        slug="speedup",
        parent=seed_sha,
        branch="work/tr-1",
        commit_sha=commit,
    )
    result = integrator.integrate("tr-1")
    tree = repo.commit_tree_sha(result.trial_commit_sha)
    blob_entry = next(
        e
        for e in repo.ls_tree(tree, recursive=True)
        if e.path == ".eden/trials/tr-1/eval.json"
    )
    payload = json.loads(repo.read_blob(blob_entry.sha).decode("utf-8"))
    assert payload["trial_id"] == "tr-1"
    assert payload["commit_sha"] == commit
