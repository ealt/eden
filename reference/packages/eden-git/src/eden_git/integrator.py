"""Reference integrator for ``spec/v0/06-integrator.md``.

Composes the ``GitRepo`` subprocess wrapper with the Phase 6
``Store`` to produce canonical ``trial/*`` commits. Given a
``success`` trial with a recorded ``commit_sha``, the integrator:

- re-validates the trial against the §2 promotion preconditions,
- re-checks §1.4 reachability via git,
- builds the §3.2 single-commit squash (worker-tip tree plus exactly
  the eval manifest at ``.eden/trials/<trial_id>/eval.json``),
- commits under the §3.3 subject line ``trial: <trial_id> <slug>``,
- creates the ``refs/heads/trial/<trial_id>-<slug>`` ref via CAS,
- writes ``trial_commit_sha`` and appends ``trial.integrated`` via
  the store's atomic ``integrate_trial`` operation,
- compensates the ref on store-side failure per §3.4.

Atomicity follows the design decision recorded in
``spec/v0/design-notes/integrator-atomicity.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from eden_contracts import Trial
from eden_storage import InvalidPrecondition, Store

from ._manifest import ManifestFieldMissing, build_manifest
from .errors import GitError
from .repo import GitRepo, Identity

__all__ = [
    "AtomicityViolation",
    "CorruptIntegrationState",
    "EvalManifestPathCollision",
    "Integrator",
    "IntegratorError",
    "IntegrationResult",
    "NotReadyForIntegration",
    "ReachabilityViolation",
]


class IntegratorError(RuntimeError):
    """Base class for integrator-level errors."""


class NotReadyForIntegration(IntegratorError):
    """Trial fails §2 promotion preconditions."""


class ReachabilityViolation(IntegratorError):
    """``commit_sha`` is not reachable from the proposal's ``parent_commits`` (§1.4)."""


class EvalManifestPathCollision(IntegratorError):
    """Worker tree already carries a file at ``.eden/trials/<id>/eval.json`` (§3.2)."""


class CorruptIntegrationState(IntegratorError):
    """§5.3: ``trial_commit_sha`` is set but the ref or tree disagrees with the trial."""


class AtomicityViolation(IntegratorError):
    """§3.4 compensating delete failed after the store-side write also failed."""

    def __init__(
        self,
        message: str,
        *,
        original: BaseException,
        rollback: BaseException,
    ) -> None:
        super().__init__(message)
        self.original = original
        self.rollback = rollback


@dataclass(frozen=True)
class IntegrationResult:
    """Return value of ``Integrator.integrate``."""

    trial_id: str
    trial_commit_sha: str
    branch: str
    already_integrated: bool


class Integrator:
    """Phase 7b reference integrator.

    Instances are light-weight and safe to reuse across trials. The
    ``store`` and ``repo`` are held by reference; callers are
    expected to scope them to the same experiment.
    """

    def __init__(
        self,
        *,
        store: Store,
        repo: GitRepo,
        author: Identity,
        committer: Identity | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._author = author
        self._committer = committer if committer is not None else author
        self._clock = clock if clock is not None else _default_clock

    def integrate(self, trial_id: str) -> IntegrationResult:
        """Promote ``trial_id`` per §3.2 / §3.4.

        Returns ``already_integrated=True`` on the idempotent no-op
        path (§5.3 bullet 1). Raises ``IntegratorError`` subclasses on
        precondition failure; ``GitError`` / ``StorageError`` from
        underlying operations propagate unchanged.
        """
        trial = self._store.read_trial(trial_id)
        proposal = self._store.read_proposal(trial.proposal_id)
        slug = proposal.slug
        branch = f"trial/{trial.trial_id}-{slug}"
        branch_ref = f"refs/heads/{branch}"

        if trial.trial_commit_sha is not None:
            return self._check_idempotent(trial, branch, branch_ref)

        self._require_promotion_preconditions(trial)
        self._require_reachability(trial)
        manifest_path = _manifest_path(trial.trial_id)
        self._require_no_manifest_collision(trial, manifest_path)

        assert trial.commit_sha is not None
        tree_sha = self._build_squash_tree(trial, manifest_path)
        commit_sha = self._commit_squash(trial, slug, tree_sha)

        self._repo.create_ref(branch_ref, commit_sha)

        try:
            self._store.integrate_trial(trial.trial_id, commit_sha)
        except BaseException as store_error:
            try:
                self._repo.delete_ref(branch_ref, expected_old_sha=commit_sha)
            except BaseException as rollback_error:
                raise AtomicityViolation(
                    f"integrator failed to commit trial {trial.trial_id!r} "
                    f"and the compensating ref-delete also failed; "
                    f"ref {branch_ref!r} is dangling at {commit_sha}",
                    original=store_error,
                    rollback=rollback_error,
                ) from store_error
            raise

        return IntegrationResult(
            trial_id=trial.trial_id,
            trial_commit_sha=commit_sha,
            branch=branch,
            already_integrated=False,
        )

    # ------------------------------------------------------------------
    # Preconditions
    # ------------------------------------------------------------------

    def _require_promotion_preconditions(self, trial: Trial) -> None:
        if trial.status != "success":
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r} status is {trial.status!r}, not 'success'"
            )
        if trial.commit_sha is None:
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r} has no commit_sha"
            )
        if trial.branch is None:
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r} has no branch"
            )

        branch_ref = f"refs/heads/{trial.branch}"
        tip = self._repo.resolve_ref(branch_ref)
        if tip is None:
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r} branch {trial.branch!r} does not exist"
            )
        if tip != trial.commit_sha and not self._repo.is_ancestor(
            trial.commit_sha, tip
        ):
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r} commit_sha {trial.commit_sha!r} "
                f"is not reachable from branch {trial.branch!r} tip {tip!r}"
            )

        metrics = trial.metrics if trial.metrics is not None else {}
        try:
            self._store.validate_metrics(metrics)
        except InvalidPrecondition as exc:
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r} metrics failed schema validation: {exc}"
            ) from exc

    def _require_reachability(self, trial: Trial) -> None:
        assert trial.commit_sha is not None
        for parent in trial.parent_commits:
            if not self._repo.is_ancestor(parent, trial.commit_sha):
                raise ReachabilityViolation(
                    f"trial {trial.trial_id!r}: parent {parent!r} is not an "
                    f"ancestor of commit_sha {trial.commit_sha!r} (§1.4)"
                )

    def _require_no_manifest_collision(
        self, trial: Trial, manifest_path: str
    ) -> None:
        assert trial.commit_sha is not None
        if self._repo.tree_entry_exists(trial.commit_sha, manifest_path):
            raise EvalManifestPathCollision(
                f"trial {trial.trial_id!r} worker tree already contains "
                f"{manifest_path!r} (§3.2 — integrator MUST NOT overwrite)"
            )

    # ------------------------------------------------------------------
    # Squash construction
    # ------------------------------------------------------------------

    def _build_squash_tree(self, trial: Trial, manifest_path: str) -> str:
        assert trial.commit_sha is not None
        try:
            manifest_bytes = build_manifest(trial)
        except ManifestFieldMissing as exc:
            raise NotReadyForIntegration(
                f"trial {trial.trial_id!r}: {exc}"
            ) from exc
        blob_sha = self._repo.write_blob(manifest_bytes)
        worker_tree = self._repo.commit_tree_sha(trial.commit_sha)
        return self._repo.write_tree_with_file(
            worker_tree, manifest_path, blob_sha, mode="100644"
        )

    def _commit_squash(self, trial: Trial, slug: str, tree_sha: str) -> str:
        message = f"trial: {trial.trial_id} {slug}\n"
        now = self._clock()
        stamp = _rfc3339(now)
        return self._repo.commit_tree(
            tree_sha,
            parents=list(trial.parent_commits),
            message=message,
            author=self._author,
            committer=self._committer,
            author_date=stamp,
            committer_date=stamp,
        )

    # ------------------------------------------------------------------
    # §5.3 idempotency
    # ------------------------------------------------------------------

    def _check_idempotent(
        self, trial: Trial, branch: str, branch_ref: str
    ) -> IntegrationResult:
        recorded = trial.trial_commit_sha
        assert recorded is not None
        ref_tip = self._repo.resolve_ref(branch_ref)
        if ref_tip is None or ref_tip != recorded:
            raise CorruptIntegrationState(
                f"trial {trial.trial_id!r} has trial_commit_sha={recorded!r} "
                f"but {branch_ref!r} resolves to {ref_tip!r} (§5.3)"
            )

        manifest_path = _manifest_path(trial.trial_id)
        try:
            expected = build_manifest(trial)
        except ManifestFieldMissing as exc:
            raise CorruptIntegrationState(
                f"trial {trial.trial_id!r} is integrated but cannot "
                f"re-derive manifest: {exc}"
            ) from exc

        # The committed trial commit + manifest blob must still be
        # present; the ref we just confirmed proves reachability.
        try:
            actual_tree = self._repo.commit_tree_sha(recorded)
            actual_blob_entry = _lookup_tree_entry(
                self._repo, actual_tree, manifest_path
            )
            actual_bytes = self._repo.read_blob(actual_blob_entry)
        except GitError as exc:
            raise CorruptIntegrationState(
                f"trial {trial.trial_id!r} is integrated but committed "
                f"manifest read failed: {exc}"
            ) from exc

        if actual_bytes != expected:
            raise CorruptIntegrationState(
                f"trial {trial.trial_id!r} integrated manifest does not match "
                f"current trial state (§5.3)"
            )

        # §3.2 tree-shape re-verification requires the worker-branch
        # tip commit (and its tree) to still be reachable in the
        # object DB. §1.3 permits deployments to delete work/*
        # branches eagerly after promotion, which — combined with
        # `git gc --prune=now` — can remove the worker commit and its
        # tree. In that case we cannot prove §5.3's "tree satisfies
        # §3.2" precondition, so the no-op path is not safe to take:
        # the recorded trial/* commit could have been rewritten
        # externally with a different tree. Raise
        # ``CorruptIntegrationState`` rather than silently treating
        # the replay as a no-op. A deployment that needs replay-
        # idempotence after eager work/* cleanup must either retain
        # the worker commit (or its tree) via a separate anchor ref,
        # or accept that replay after cleanup requires operator
        # intervention.
        assert trial.commit_sha is not None
        if not self._repo.commit_exists(trial.commit_sha):
            raise CorruptIntegrationState(
                f"trial {trial.trial_id!r} is integrated but its worker "
                f"commit {trial.commit_sha!r} is no longer reachable; "
                f"§3.2 tree-shape verification cannot complete "
                f"(§1.3 permits work/* cleanup, but §5.3 replay then "
                f"requires operator intervention)"
            )
        self._require_squash_tree_matches(
            trial.commit_sha, actual_tree, actual_blob_entry, manifest_path
        )

        return IntegrationResult(
            trial_id=trial.trial_id,
            trial_commit_sha=recorded,
            branch=branch,
            already_integrated=True,
        )

    def _require_squash_tree_matches(
        self,
        worker_commit_sha: str,
        actual_tree: str,
        actual_blob_entry: str,
        manifest_path: str,
    ) -> None:
        """Verify ``actual_tree`` equals worker-tip tree + only the manifest.

        Re-derives the expected tree from the worker branch tip and the
        committed manifest blob, then compares tree SHAs. Equality
        implies the §3.2 rule holds — only the manifest path differs.
        Callers must have already verified ``worker_commit_sha`` is
        still reachable in the object DB.
        """
        worker_tree = self._repo.commit_tree_sha(worker_commit_sha)
        expected_tree = self._repo.write_tree_with_file(
            worker_tree, manifest_path, actual_blob_entry, mode="100644"
        )
        if expected_tree != actual_tree:
            raise CorruptIntegrationState(
                f"trial {worker_commit_sha!r} integrated tree does not match "
                f"worker-tip tree plus manifest (§3.2 / §5.3)"
            )


def _manifest_path(trial_id: str) -> str:
    return f".eden/trials/{trial_id}/eval.json"


def _default_clock() -> datetime:
    return datetime.now(tz=UTC)


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _lookup_tree_entry(repo: GitRepo, tree_sha: str, path: str) -> str:
    """Resolve ``path`` under ``tree_sha`` to the blob SHA at that path."""
    for entry in repo.ls_tree(tree_sha, path, recursive=True):
        if entry.path == path and entry.type == "blob":
            return entry.sha
    raise GitError(
        ["ls-tree", "-r", tree_sha, "--", path],
        0,
        "",
        f"path {path!r} not found in tree {tree_sha!r}",
    )
