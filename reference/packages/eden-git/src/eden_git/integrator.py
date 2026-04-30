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
from eden_storage import InvalidPrecondition, NotFound, Store

from ._manifest import ManifestFieldMissing, build_manifest
from .errors import GitError, GitTransportError, RefRefused
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
    """§3.4 atomicity broken; operator intervention required.

    Raised in two distinct-but-related cases that both leave the
    three-artifact invariant in a state the Integrator cannot safely
    reconcile on its own:

    - The store-side ``integrate_trial`` call failed **and** the
      compensating ref-delete also failed; ``rollback`` is set to
      the rollback-side error.
    - The store-side call raised ``InvalidPrecondition`` for the
      different-SHA divergence branch of §5 same-value idempotency
      (the store already has a different ``trial_commit_sha``
      recorded). The sole-writer rule (§1.2) has been violated
      upstream; ``rollback`` is ``None`` because there is nothing
      safe to compensate.
    """

    def __init__(
        self,
        message: str,
        *,
        original: BaseException,
        rollback: BaseException | None = None,
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

        # Phase 10d follow-up B: when the integrator's clone has an
        # origin remote, fetch the implementer-pushed work/* branch
        # before §2 reachability checks run. The startup-only
        # fetch_all_heads can race against work/* refs the
        # implementer pushes AFTER orchestrator startup; the
        # promotion path must refresh local state per the long-lived-
        # clone freshness rule (plan §D.7d).
        if trial.branch is not None and _has_origin(self._repo):
            try:
                self._repo.fetch_ref(f"refs/heads/{trial.branch}")
            except Exception:  # noqa: BLE001 — git/transport-shaped
                # Fall through to the existing reachability check;
                # if the branch genuinely isn't on the remote OR
                # we couldn't fetch it, the next check raises
                # NotReadyForIntegration with a precise diagnostic.
                pass

        self._require_promotion_preconditions(trial)
        self._require_reachability(trial)
        manifest_path = _manifest_path(trial.trial_id)
        self._require_no_manifest_collision(trial, manifest_path)

        assert trial.commit_sha is not None
        tree_sha = self._build_squash_tree(trial, manifest_path)
        commit_sha = self._commit_squash(trial, slug, tree_sha)

        # Step 1: local CAS-guarded ref write.
        self._repo.create_ref(branch_ref, commit_sha)

        # Step 2: publish to remote (if origin is configured).
        # Phase 10d follow-up B §D.6: when the integrator's repo has
        # an `origin` remote (the Gitea cutover), publish the ref
        # there before committing the store-side trial state. Skip
        # the push for repos with no origin (the local-only test
        # path stays a single-step integration).
        published = False
        if _has_origin(self._repo):
            try:
                self._repo.push_ref(branch_ref, expected_old_sha=self._repo.zero_oid())
                published = True
            except RefRefused as push_rejected:
                # Definite remote rejection — only step-1's local
                # ref exists. Roll back local; do NOT touch remote
                # (the remote either has a different SHA on the same
                # ref, in which case another integrator wrote a
                # different commit and our ref does not belong, or
                # the remote refused for a hook-level reason).
                self._rollback_local_only(
                    branch_ref, commit_sha, push_rejected
                )
                raise
            except GitTransportError as push_transport:
                # Transport-indeterminate: the remote may or may
                # not have applied the ref. Disambiguate via
                # ls-remote per §D.7d.
                published = self._reconcile_indeterminate_push(
                    branch_ref, commit_sha, push_transport
                )

        # Step 3: store integrate_trial (atomic with trial.integrated).
        try:
            self._store.integrate_trial(trial.trial_id, commit_sha)
        except BaseException as store_error:
            # Distinguish the different-SHA divergence branch of §5
            # same-value idempotency (sole-writer §1.2 already
            # violated; no safe compensation) from other synchronous
            # failures (compensate the ref per §3.4). Authoritative
            # test is a post-failure read of the trial: if it now
            # carries a *different* trial_commit_sha, divergence is
            # confirmed and the ref we just wrote must not be
            # deleted — the other integrator's artifacts, whatever
            # their state, take priority, and the situation requires
            # operator intervention.
            try:
                post_failure = self._store.read_trial(trial.trial_id)
            except BaseException:
                post_failure = None
            if (
                post_failure is not None
                and post_failure.trial_commit_sha is not None
                and post_failure.trial_commit_sha != commit_sha
            ):
                raise AtomicityViolation(
                    f"integrator for trial {trial.trial_id!r} observed a "
                    f"different trial_commit_sha {post_failure.trial_commit_sha!r} "
                    f"already recorded; §1.2 sole-writer rule violated upstream. "
                    f"Ref {branch_ref!r} pointing at {commit_sha} has NOT been "
                    f"compensated; operator intervention required.",
                    original=store_error,
                ) from store_error
            # Step 4a: remote compensating delete (if we published).
            if published:
                try:
                    self._repo.delete_remote_ref(
                        branch_ref, expected_sha=commit_sha
                    )
                except BaseException as remote_delete_error:
                    # Per §D.7d, this is the case where §D.7c's
                    # startup sweep is the backstop. Annotate the
                    # AtomicityViolation so the operator can see
                    # both halves of the failure chain; do NOT skip
                    # step 4b (local rollback still runs so the next
                    # in-process retry is clean).
                    try:
                        self._repo.delete_ref(
                            branch_ref, expected_old_sha=commit_sha
                        )
                    except BaseException as local_rollback_error:
                        raise AtomicityViolation(
                            f"integrator failed to commit trial "
                            f"{trial.trial_id!r}, the remote compensating "
                            f"delete failed, AND the local rollback "
                            f"failed; ref {branch_ref!r} is dangling at "
                            f"{commit_sha} both locally and remotely",
                            original=store_error,
                            rollback=local_rollback_error,
                        ) from store_error
                    raise AtomicityViolation(
                        f"integrator failed to commit trial "
                        f"{trial.trial_id!r} and the remote compensating "
                        f"delete failed; remote ref {branch_ref!r} "
                        f"dangling at {commit_sha} until the next "
                        f"integrator-startup remote-orphan sweep "
                        f"(§D.7c) cleans it up",
                        original=store_error,
                        rollback=remote_delete_error,
                    ) from store_error
            # Step 4b: local compensating delete.
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

    def _rollback_local_only(
        self,
        branch_ref: str,
        commit_sha: str,
        push_error: BaseException,
    ) -> None:
        """Roll back step 1 after a definite remote rejection (step 2)."""
        try:
            self._repo.delete_ref(branch_ref, expected_old_sha=commit_sha)
        except BaseException as rollback_error:
            raise AtomicityViolation(
                f"integrator's push to remote was rejected and the "
                f"local rollback also failed; local ref {branch_ref!r} "
                f"is dangling at {commit_sha}",
                original=push_error,
                rollback=rollback_error,
            ) from push_error

    def _reconcile_indeterminate_push(
        self,
        branch_ref: str,
        commit_sha: str,
        push_error: GitTransportError,
    ) -> bool:
        """Disambiguate a transport-failed push via ls-remote read-back.

        Returns ``True`` if the remote DID accept our ref (so step 4a
        compensating-delete must run on store failure), ``False`` if
        the remote did NOT accept (only step 4b local rollback). On
        ls-remote-also-fails, raises :class:`GitTransportError` —
        the caller treats this as a §D.7d "defer to startup sweep"
        and rolls back local only.
        """
        try:
            refs = self._repo.ls_remote(branch_ref)
        except GitTransportError:
            # Both push and ls-remote transport-failed. Local-only
            # rollback is the safest action; the §D.7c startup
            # sweep is the backstop for any remote orphan.
            self._rollback_local_only(branch_ref, commit_sha, push_error)
            raise
        remote_sha: str | None = None
        for name, sha in refs:
            if name == branch_ref:
                remote_sha = sha
                break
        if remote_sha is None:
            # Push transport-failed and the ref is not on the remote
            # — local rollback only.
            self._rollback_local_only(branch_ref, commit_sha, push_error)
            raise push_error
        if remote_sha == commit_sha:
            # Push DID land; ack just got lost.
            return True
        # Remote has a DIFFERENT SHA — another integrator won the
        # race. RefRefused semantics: roll back local only.
        self._rollback_local_only(branch_ref, commit_sha, push_error)
        raise RefRefused(
            ["push", "origin", branch_ref],
            push_error.returncode,
            push_error.stdout,
            f"remote ref {branch_ref!r} resolved to {remote_sha!r} after "
            f"transport-indeterminate push; another integrator integrated "
            f"a different commit on the same trial id",
        ) from push_error

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

    # ------------------------------------------------------------------
    # Phase 10d follow-up B §D.7c — startup remote-orphan reconciliation
    # ------------------------------------------------------------------

    def reconcile_remote_orphans(self) -> list[str]:
        """Sweep ``trial/*`` refs on the remote that lack store-side trials.

        Recovery rule for the §D.6 step-4a-failed and the
        push-transport-then-ls-remote-also-failed cases. Only meaningful
        when the integrator's repo has an ``origin`` remote.

        Returns the list of remote refs that were deleted. Refs whose
        store-side trial cannot be derived (malformed trial commit) are
        left in place and logged via raised ``CorruptIntegrationState``
        — the operator decides; the integrator does not attempt
        corrective writes for them.
        """
        deleted: list[str] = []
        if not _has_origin(self._repo):
            return deleted
        try:
            remote_refs = self._repo.ls_remote("refs/heads/trial/*")
        except GitTransportError:
            # Gitea unreachable at startup — caller exits non-zero
            # and compose's restart loop retries. Don't raise here
            # because we don't want to block startup if the only
            # transient is reconciliation; the next startup tries
            # again.
            return deleted
        for ref, ref_sha in remote_refs:
            trial_id = self._recover_trial_id_from_remote_commit(ref, ref_sha)
            if trial_id is None:
                continue
            try:
                trial = self._store.read_trial(trial_id)
            except NotFound:
                # Authoritative: the trial truly doesn't exist in the
                # store. Safe to delete the orphan ref.
                trial = None
            except Exception:  # noqa: BLE001
                # Indeterminate: transport / server failure. We MUST
                # NOT delete a remote ref that may correspond to a
                # valid integrated trial — chapter 6 §3.4 only
                # authorizes the delete when the store is
                # authoritatively missing the trial. Skip and let
                # the next startup retry.
                continue
            if trial is None or trial.trial_commit_sha is None:
                try:
                    self._repo.delete_remote_ref(ref, expected_sha=ref_sha)
                    deleted.append(ref)
                except Exception:  # noqa: BLE001 — best-effort
                    # The next startup tries again. Don't raise.
                    pass
        return deleted

    def _recover_trial_id_from_remote_commit(
        self, ref: str, ref_sha: str
    ) -> str | None:
        """Recover the trial_id from a remote trial-commit's tree.

        Per chapter 6 §3.2 the squash commit's tree must contain
        exactly one ``.eden/trials/<trial_id>/eval.json`` entry. The
        spec treats trial_id as opaque (chapter 2 §1.3), so ref-name
        parsing is NOT used.

        Fetches the commit locally if not already present, then reads
        ``ls-tree --name-only <sha> .eden/trials/`` and recovers the
        single subdir name.

        Returns ``None`` if the commit isn't retrievable, the tree
        lacks ``.eden/trials/``, or the entry shape is malformed
        (zero or multiple subdirs).
        """
        # Make sure we have the commit locally so we can read its tree.
        if not self._repo.commit_exists(ref_sha):
            try:
                self._repo.fetch_ref(ref)
            except (GitTransportError, GitError):
                return None
            if not self._repo.commit_exists(ref_sha):
                return None
        try:
            tree_sha = self._repo.commit_tree_sha(ref_sha)
        except GitError:
            return None
        # Read `.eden/trials/` directory entries via ls-tree (one-level).
        try:
            entries = self._repo.ls_tree(
                tree_sha, ".eden/trials/", recursive=False
            )
        except GitError:
            return None
        # Filter to direct children (subdirectories — the trial_id dirs).
        subdirs = [
            entry for entry in entries
            if entry.type == "tree"
            and entry.path.startswith(".eden/trials/")
            and entry.path.count("/") == 2
        ]
        if len(subdirs) != 1:
            # Malformed: zero or multiple. Per the residual-risk note
            # in plan §F-T, fail closed.
            return None
        # entry.path is ".eden/trials/<trial_id>"
        trial_id = subdirs[0].path.rsplit("/", 1)[1]
        if not trial_id:
            return None
        return trial_id

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


def _has_origin(repo: GitRepo) -> bool:
    """Return ``True`` if the repo has an ``origin`` remote configured.

    Used by ``Integrator.integrate`` to gate the publish-to-remote
    step. Repos initialized via ``GitRepo.init_bare`` (the local-only
    test path) have no origin and skip the push entirely.
    """
    try:
        result = repo._run(["remote"], check=False)
    except Exception:  # noqa: BLE001
        return False
    return "origin" in result.stdout.split()


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
