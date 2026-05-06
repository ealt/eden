"""Integration tests for the Phase 10d follow-up B integrator flow.

Drives the public ``Integrator.integrate(variant_id)`` method through
the four-step ladder + each failure mode, using a real local-file
"remote" + the in-memory Store.

Per the AGENTS.md "test the actual code path" pitfall, every test
goes through ``Integrator.integrate`` (not helper methods directly)
so the production code path's wiring is what the assertions check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema, Idea, Variant
from eden_git import (
    AtomicityViolation,
    GitRepo,
    Identity,
    Integrator,
    RefRefused,
    TreeEntry,
)
from eden_git.errors import GitTransportError
from eden_storage import InMemoryStore, Store
from eden_storage.errors import DispatchError

TEST_AUTHOR = Identity(name="EDEN Test", email="test@eden.example")
FIXED_DATE = "2026-04-23T00:00:00Z"
EXPERIMENT_ID = "exp-test"


def _setup(
    tmp_path: Path,
    *,
    register_origin: bool = True,
) -> tuple[InMemoryStore, GitRepo, GitRepo, str]:
    """Build a Store + remote bare repo + working clone.

    Returns ``(store, remote, working_clone, base_sha)``. The working
    clone has ``origin`` pointed at the remote when ``register_origin``
    is True (the default — that's what the production integrator
    uses). Set False to exercise the local-only fallback.
    """
    remote = GitRepo.init_bare(tmp_path / "remote.git")
    blob = remote.write_blob(b"seed\n")
    tree = remote.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
    )
    base = remote.commit_tree(
        tree,
        parents=[],
        message="seed",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    remote.create_ref("refs/heads/main", base)

    if register_origin:
        clone = GitRepo.clone_from(
            url=f"file://{remote.path}",
            dest=tmp_path / "clone.git",
            bare=True,
        )
    else:
        clone = remote  # tests that don't want a remote drive directly

    schema = EvaluationSchema({"score": "real"})
    store = InMemoryStore(experiment_id=EXPERIMENT_ID, evaluation_schema=schema)
    return store, remote, clone, base


def _force_variant_success(
    store: Store,
    variant_id: str,
    *,
    commit_sha: str,
    evaluation: dict,
) -> Variant:
    """Backdoor: poke the variant to ``success`` directly.

    Mirrors the same shortcut ``test_integrator.py`` uses — these
    tests exercise the integrator's flow, not the full task lifecycle.
    """
    from eden_storage._base import _validated_update

    variant = store.read_variant(variant_id)
    updated = _validated_update(
        variant,
        status="success",
        commit_sha=commit_sha,
        evaluation=evaluation,
        completed_at="2026-04-23T02:00:00Z",
    )
    store._variants[variant_id] = updated  # type: ignore[attr-defined]
    return updated


def _seed_success_variant(
    *,
    store: InMemoryStore,
    repo: GitRepo,
    base_sha: str,
    slug: str = "p0",
    variant_id: str = "variant-aaa",
    idea_id: str = "idea-aaa",
) -> Variant:
    """Persist an Idea + work-branch + Variant(status="success")."""
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=EXPERIMENT_ID,
            slug=slug,
            priority=1.0,
            parent_commits=[base_sha],
            artifacts_uri=f"file:///idea/{idea_id}",
            state="drafting",
            created_at=FIXED_DATE,
        )
    )

    # Build a worker commit on the repo.
    blob = repo.write_blob(b"work\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="WORK")]
    )
    work_commit = repo.commit_tree(
        tree,
        parents=[base_sha],
        message="work",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    branch = f"work/{slug}-{variant_id}"
    repo.create_ref(f"refs/heads/{branch}", work_commit)
    if "origin" in repo._run(["remote"], check=False).stdout.split():
        repo.push_ref(f"refs/heads/{branch}")

    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=EXPERIMENT_ID,
            idea_id=idea_id,
            status="starting",
            parent_commits=[base_sha],
            branch=branch,
            started_at=FIXED_DATE,
        )
    )
    return _force_variant_success(
        store, variant_id, commit_sha=work_commit, evaluation={"score": 1.0}
    )


def _make_integrator(store: InMemoryStore, repo: GitRepo) -> Integrator:
    return Integrator(
        store=store,
        repo=repo,
        author=TEST_AUTHOR,
        committer=TEST_AUTHOR,
        clock=lambda: datetime(2026, 4, 23, 0, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------- happy path


def test_integrate_fetches_work_branch_pushed_after_startup(
    tmp_path: Path,
) -> None:
    """Integrator integrates from a clone that didn't have the work/*
    branch at startup — the executor pushed it later.

    Regression for the round-1 codex finding: a startup-only
    fetch_all_heads misses any work/* ref that an executor
    pushes AFTER orchestrator startup. Integrator.integrate's
    chapter-6 §2 reachability check would then fail with
    'branch does not exist' even though the ref is on Gitea. The
    integrator must fetch variant.branch before §2 checks.
    """
    # Build the test scenario: integrator has a private clone, but
    # the executor's work/* branch doesn't exist locally yet —
    # it lives only on the remote. The integrator clone simulates
    # a long-lived process whose startup fetch happened before the
    # executor's push.
    store, remote, integrator_clone, base = _setup(tmp_path)

    # Set up a SECOND clone (the executor's) and have it push
    # a work/* commit to the remote.
    executor_clone = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "executor-clone.git",
        bare=True,
    )
    idea_id = "idea-late-push"
    variant_id = "variant-late-push"
    slug = "late"
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=EXPERIMENT_ID,
            slug=slug,
            priority=1.0,
            parent_commits=[base],
            artifacts_uri=f"file:///idea/{idea_id}",
            state="drafting",
            created_at=FIXED_DATE,
        )
    )
    blob = executor_clone.write_blob(b"late\n")
    tree = executor_clone.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="LATE")]
    )
    work_commit = executor_clone.commit_tree(
        tree, parents=[base], message="late",
        author=TEST_AUTHOR, author_date=FIXED_DATE, committer_date=FIXED_DATE,
    )
    branch = f"work/{slug}-{variant_id}"
    executor_clone.create_ref(f"refs/heads/{branch}", work_commit)
    executor_clone.push_ref(f"refs/heads/{branch}")

    # Integrator clone does NOT have this branch yet.
    assert integrator_clone.resolve_ref(f"refs/heads/{branch}") is None

    # Now stand up the variant in the store (status=success with the
    # commit_sha) — same backdoor as the other tests use.
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=EXPERIMENT_ID,
            idea_id=idea_id,
            status="starting",
            parent_commits=[base],
            branch=branch,
            started_at=FIXED_DATE,
        )
    )
    _force_variant_success(
        store, variant_id, commit_sha=work_commit, evaluation={"score": 1.0}
    )

    integrator = _make_integrator(store, integrator_clone)
    result = integrator.integrate(variant_id)

    # Integration succeeds: the integrator fetched the executor's
    # work/* before the §2 reachability check ran.
    assert not result.already_integrated
    branch_ref = f"refs/heads/variant/{variant_id}-{slug}"
    assert remote.resolve_ref(branch_ref) == result.variant_commit_sha


def test_integrate_publishes_to_remote(tmp_path: Path) -> None:
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    result = integrator.integrate(variant.variant_id)

    assert not result.already_integrated
    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    # Local AND remote both have the variant ref at the new commit.
    assert clone.resolve_ref(branch_ref) == result.variant_commit_sha
    assert remote.resolve_ref(branch_ref) == result.variant_commit_sha
    # Store has the variant integrated.
    after = store.read_variant(variant.variant_id)
    assert after.variant_commit_sha == result.variant_commit_sha


# ---------------------------------------------------------------- step 2: definite rejection


def test_integrate_rolls_back_local_on_remote_rejection(tmp_path: Path) -> None:
    """Push race: another integrator already wrote a different SHA at the same variant ref."""
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    # Pre-publish a different commit at the variant ref on the remote so
    # our --force-with-lease=zero push is rejected.
    blob = remote.write_blob(b"intruder\n")
    tree = remote.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="THEIRS")]
    )
    intruder = remote.commit_tree(
        tree,
        parents=[base],
        message="intruder",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    remote.create_ref(branch_ref, intruder)

    with pytest.raises(RefRefused):
        integrator.integrate(variant.variant_id)

    # Local ref rolled back; remote untouched.
    assert clone.resolve_ref(branch_ref) is None
    assert remote.resolve_ref(branch_ref) == intruder
    # Store still has variant as `success` (no integrate_variant happened).
    after = store.read_variant(variant.variant_id)
    assert after.variant_commit_sha is None


# ---------------------------------------------------------------- step 3: store failure after push


def test_integrate_compensates_remote_on_store_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 3 fails after step 2 succeeded: BOTH 4a and 4b run."""
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    # Force store.integrate_variant to fail.
    def _boom(*args: object, **kwargs: object) -> None:
        raise DispatchError("simulated store outage")
    monkeypatch.setattr(store, "integrate_variant", _boom)

    with pytest.raises(DispatchError):
        integrator.integrate(variant.variant_id)

    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    # Both local AND remote refs deleted (4a + 4b).
    assert clone.resolve_ref(branch_ref) is None
    assert remote.resolve_ref(branch_ref) is None
    # Store still has variant as `success` (no integrate_variant committed).
    after = store.read_variant(variant.variant_id)
    assert after.variant_commit_sha is None


def test_integrate_atomicity_violation_when_remote_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 4a fails: AtomicityViolation raised, local 4b still runs."""
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    def _boom(*args: object, **kwargs: object) -> None:
        raise DispatchError("simulated store outage")
    monkeypatch.setattr(store, "integrate_variant", _boom)

    # Force the compensating remote-delete to fail too.
    real_delete = clone.delete_remote_ref

    def _boom_delete(*args: object, **kwargs: object) -> None:
        raise GitTransportError(
            ["push", "origin", "--delete", "stub"],
            128, "", "fatal: unable to access (simulated)",
        )
    monkeypatch.setattr(clone, "delete_remote_ref", _boom_delete)

    with pytest.raises(AtomicityViolation) as exc:
        integrator.integrate(variant.variant_id)

    assert isinstance(exc.value.original, DispatchError)
    assert isinstance(exc.value.rollback, GitTransportError)
    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    # Local 4b still ran (best-effort consistency for the next retry).
    assert clone.resolve_ref(branch_ref) is None
    # The remote ref is the orphan that §D.7c reconciliation cleans up.
    real_delete_result = remote.resolve_ref(branch_ref)
    assert real_delete_result is not None
    # Restore for cleanup.
    monkeypatch.setattr(clone, "delete_remote_ref", real_delete)


# ---------------------------------------------------------------- §D.7c reconciliation


def test_reconcile_remote_orphans_deletes_orphan_with_no_store_record(
    tmp_path: Path,
) -> None:
    """An orphan remote variant/* with no Store.read_variant → deleted."""
    store, remote, clone, base = _setup(tmp_path)
    # Forge an orphan: a commit whose tree contains
    # .eden/variants/orphan-variant-id/evaluation.json, pushed to a variant/* ref,
    # with NO matching variant in the store.
    blob = clone.write_blob(b'{"variant_id": "orphan-variant-id"}\n')
    eval_path = ".eden/variants/orphan-variant-id/evaluation.json"
    worker_blob = clone.write_blob(b"worker\n")
    worker_tree = clone.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=worker_blob, path="WORK")]
    )
    full_tree = clone.write_tree_with_file(
        worker_tree, eval_path, blob, mode="100644"
    )
    orphan_commit = clone.commit_tree(
        full_tree,
        parents=[base],
        message="variant: orphan-variant-id porphan\n",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    branch_ref = "refs/heads/variant/orphan-variant-id-porphan"
    clone.create_ref(branch_ref, orphan_commit)
    clone.push_ref(branch_ref)
    assert remote.resolve_ref(branch_ref) == orphan_commit

    integrator = _make_integrator(store, clone)
    deleted = integrator.reconcile_remote_orphans()
    assert branch_ref in deleted
    assert remote.resolve_ref(branch_ref) is None


def test_reconcile_remote_orphans_leaves_valid_integrated_refs(
    tmp_path: Path,
) -> None:
    """A remote variant/* whose Store.read_variant has variant_commit_sha → kept."""
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)
    integrator.integrate(variant.variant_id)
    # Now the variant is properly integrated. Reconciling must leave
    # the ref alone.
    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    deleted = integrator.reconcile_remote_orphans()
    assert deleted == []
    assert remote.resolve_ref(branch_ref) is not None


def test_reconcile_skips_malformed_variant_commits(tmp_path: Path) -> None:
    """A remote variant/* whose tree lacks .eden/variants/<id>/evaluation.json
    is left alone (fail closed; the operator decides)."""
    store, remote, clone, base = _setup(tmp_path)
    # Forge a malformed "variant" commit whose tree has no .eden/variants/.
    blob = clone.write_blob(b"plain\n")
    tree = clone.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
    )
    bad_commit = clone.commit_tree(
        tree,
        parents=[base],
        message="not actually a variant",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    branch_ref = "refs/heads/variant/zz-malformed"
    clone.create_ref(branch_ref, bad_commit)
    clone.push_ref(branch_ref)

    integrator = _make_integrator(store, clone)
    deleted = integrator.reconcile_remote_orphans()
    # Malformed: the integrator can't recover the variant_id, so it
    # leaves the ref alone.
    assert branch_ref not in deleted
    assert remote.resolve_ref(branch_ref) == bad_commit


# ---------------------------------------------------------------- local-only fallback


def test_integrate_indeterminate_push_remote_landed_runs_4a_and_4b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2 transport-fails AFTER the remote applied the ref → 4a + 4b.

    Drives `_reconcile_indeterminate_push`'s "remote == our SHA"
    branch: push raises GitTransportError but the remote actually
    accepted the ref before the ack was lost. The ladder must
    detect this via ls_remote and run BOTH compensating deletes
    when the subsequent store.integrate_variant fails.
    """
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    real_push = clone.push_ref
    real_lsremote = clone.ls_remote

    def _push_then_transport_fail(ref, *args, **kwargs):
        # Actually do the push (so the remote DID apply the ref)
        # then raise as if the ack was lost.
        real_push(ref, *args, **kwargs)
        raise GitTransportError(
            ["push", "origin", ref], 128, "", "fatal: unable to access (simulated)"
        )

    monkeypatch.setattr(clone, "push_ref", _push_then_transport_fail)

    def _boom(*args: object, **kwargs: object) -> None:
        raise DispatchError("simulated store outage")
    monkeypatch.setattr(store, "integrate_variant", _boom)

    # When 4a + 4b both succeed, the integrator re-raises the
    # original store error (DispatchError) — AtomicityViolation
    # is only raised when one of the compensating deletes ITSELF
    # fails. This proves the integrator detected the push had
    # landed (via ls_remote) and ran BOTH deletes successfully.
    with pytest.raises(DispatchError):
        integrator.integrate(variant.variant_id)

    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    # Local AND remote must both be cleaned up: 4a deleted the
    # remote (the integrator detected the push had landed via
    # ls_remote), 4b deleted the local.
    assert clone.resolve_ref(branch_ref) is None
    assert remote.resolve_ref(branch_ref) is None

    # Restore so monkeypatch's teardown cleanup works.
    monkeypatch.setattr(clone, "push_ref", real_push)
    monkeypatch.setattr(clone, "ls_remote", real_lsremote)


def test_integrate_indeterminate_push_remote_absent_runs_4b_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2 transport-fails BEFORE the remote applied the ref → 4b only.

    Drives `_reconcile_indeterminate_push`'s "remote absent" branch:
    push raises GitTransportError and ls_remote shows the ref
    didn't land. Only the local rollback runs.
    """
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    def _push_transport_fail_no_apply(ref, *args, **kwargs):
        # Don't actually push; raise as if it never reached the
        # server.
        raise GitTransportError(
            ["push", "origin", ref], 128, "", "fatal: unable to access (simulated)"
        )

    monkeypatch.setattr(clone, "push_ref", _push_transport_fail_no_apply)

    with pytest.raises(GitTransportError):
        integrator.integrate(variant.variant_id)

    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    # Local rolled back; remote was never touched (and ls_remote
    # confirmed it).
    assert clone.resolve_ref(branch_ref) is None
    assert remote.resolve_ref(branch_ref) is None


def test_integrate_indeterminate_push_remote_diverged_runs_4b_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2 transport-fails AND remote has a different SHA → 4b only.

    Drives `_reconcile_indeterminate_push`'s "remote at different
    SHA" branch: another integrator integrated a different commit
    on the same variant id, our push (which would have failed
    `--force-with-lease=zero` anyway) racially encountered a
    transport failure, ls_remote shows a different SHA. Mapped
    to RefRefused semantics: 4b only.
    """
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)

    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    # Pre-publish a different SHA on the remote.
    blob = remote.write_blob(b"intruder\n")
    tree = remote.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="THEIRS")]
    )
    intruder = remote.commit_tree(
        tree, parents=[base], message="intruder",
        author=TEST_AUTHOR, author_date=FIXED_DATE, committer_date=FIXED_DATE,
    )
    remote.create_ref(branch_ref, intruder)

    def _push_transport_fail(ref, *args, **kwargs):
        raise GitTransportError(
            ["push", "origin", ref], 128, "", "fatal: unable to access (simulated)"
        )

    monkeypatch.setattr(clone, "push_ref", _push_transport_fail)

    with pytest.raises(RefRefused):
        integrator.integrate(variant.variant_id)

    # Local rolled back; remote untouched.
    assert clone.resolve_ref(branch_ref) is None
    assert remote.resolve_ref(branch_ref) == intruder


def test_reconcile_skips_orphan_when_store_read_fails_indeterminately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indeterminate Store.read_variant → leave the remote ref alone.

    Reviewer round-0 finding: the original code treated ANY
    Store.read_variant exception as 'variant missing' and deleted
    the remote ref. Under the wire client, transport failures are
    indistinguishable from NotFound — so a transient store outage
    during startup reconciliation could destroy a valid integrated
    ref. Only NotFound authorizes deletion now.
    """
    store, remote, clone, base = _setup(tmp_path)
    variant = _seed_success_variant(store=store, repo=clone, base_sha=base)
    integrator = _make_integrator(store, clone)
    integrator.integrate(variant.variant_id)
    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    assert remote.resolve_ref(branch_ref) is not None

    # Simulate a transport-level Store failure (e.g., the
    # task-store-server is briefly down at orchestrator startup).
    # NOT NotFound — so the integrator must NOT delete the ref.
    def _boom(*args: object, **kwargs: object) -> None:
        raise DispatchError("store transport hiccup")
    monkeypatch.setattr(store, "read_variant", _boom)

    deleted = integrator.reconcile_remote_orphans()
    assert deleted == []
    # The valid integrated ref MUST still be on the remote.
    assert remote.resolve_ref(branch_ref) is not None


def test_integrate_without_origin_skips_remote_publish(tmp_path: Path) -> None:
    """Repos with no `origin` remote use the local-only flow (back-compat
    for pre-cutover tests). All existing eden-git tests exercise this
    path; this test pins it explicitly."""
    store, _, repo, base = _setup(tmp_path, register_origin=False)
    # repo here is the bare repo itself (no origin).
    variant = _seed_success_variant(store=store, repo=repo, base_sha=base)
    integrator = _make_integrator(store, repo)
    result = integrator.integrate(variant.variant_id)
    assert not result.already_integrated
    branch_ref = f"refs/heads/variant/{variant.variant_id}-p0"
    assert repo.resolve_ref(branch_ref) == result.variant_commit_sha
    after = store.read_variant(variant.variant_id)
    assert after.variant_commit_sha == result.variant_commit_sha
