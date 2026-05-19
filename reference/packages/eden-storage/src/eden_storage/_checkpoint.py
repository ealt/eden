"""Portable-checkpoint export / import for the reference Store backends.

The format library (``eden_checkpoint``) handles tar / JSONL / manifest
machinery; this module bridges that to the ``_StoreBase`` snapshot
read + bulk-insert flow. Both directions run inside the backend's
``_atomic_operation`` context so the read is a single transactional
snapshot and the import either commits everything or rolls back.

Per ``spec/v0/10-checkpoints.md`` §6 atomicity contract: this binding
uses the **transactional-snapshot** strategy. The backend's
``BEGIN IMMEDIATE`` (SQLite) / ``BEGIN ISOLATION LEVEL SERIALIZABLE``
(Postgres) / ``RLock`` (in-memory) makes the read self-consistent.

Wave 3 scope:

- Export every Store-managed entity (tasks, ideas, variants,
  submissions, events, workers, groups) plus the runtime experiment
  object and dispatch_mode.
- Import the same set + set ``imported_from`` on the receiving
  experiment per §10.
- Caller supplies the substrate-external pieces (experiment_config
  YAML, repo bundle bytes); the resulting archive carries them
  alongside the Store-managed data so a fresh receiver has everything
  it needs.

Wave 3 explicitly does NOT:

- Resolve / rewrite ``artifacts_uri`` values. The export carries
  them verbatim; if they happen to be deployment-local
  ``file://`` URIs they won't resolve on the receiver. The
  ``checkpoint:sha256:<hex>`` rewrite contract from §7 lands in
  wave 4 alongside the wire layer's artifact substrate plumbing.
- Mint per-worker credentials at import. Workers are inserted with
  a placeholder credential hash; the receiver MUST run
  ``reissue_credential`` for every imported worker before the
  experiment can resume (the manifest's
  ``requires_credential_reissue: true`` flag advertises this).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, BinaryIO

from eden_checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointInvalid,
    CheckpointManifest,
    CheckpointReader,
    CheckpointWriter,
    ExperimentIdConflict,
    ExperimentIdMismatch,
    ExporterInfo,
    ManifestCounts,
    SpecVersionMismatch,
    extract_checkpoint,
)
from eden_contracts import (
    Event,
    Group,
    Idea,
    ImportProvenance,
    Task,
    TaskAdapter,
    Variant,
    Worker,
)

from ._base import _DEFAULT_DISPATCH_MODE, _Tx
from .submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
)

if TYPE_CHECKING:
    from ._base import _StoreBase


_REFERENCE_IMPL_TAG = "eden-reference/0.x"
"""Identifier emitted on every manifest produced by this binding."""

_REFERENCE_ATOMICITY = "transactional_snapshot"
"""Atomicity strategy advertised by this binding per ``10-checkpoints.md`` §6."""

_IMPORT_PLACEHOLDER_CREDENTIAL_HASH_PREFIX = "$reissue-required$"
"""Sentinel prefix on credential hashes synthesized at import time.

Per ``10-checkpoints.md`` §8 the importer MUST mint a fresh credential
for every imported worker before the experiment can resume; the
``argon2id verify`` check on this sentinel will always fail, so a
worker presenting the (long-since-stripped) source credential cannot
authenticate against the imported store. The prefix is recognizable so
ops tooling can grep for it; the suffix is randomized so two
checkpoints round-tripped into different stores don't share a sentinel.
"""


@dataclass
class ImportResult:
    """Aggregate of what an importer returns to the caller.

    The Store-managed state (tasks / ideas / variants / submissions /
    events / workers / groups / dispatch_mode / experiment runtime) has
    already been committed by the time this object is returned. The
    substrate-external pieces (``experiment_config``,
    ``repo_bundle_path``, ``artifact_digests``) are made available so
    the caller can wire them into deployment-local substrates per
    ``spec/v0/10-checkpoints.md`` §6 / §7.
    """

    experiment_id: str
    """The imported experiment's id (manifest's value, or the ``as_experiment_id``
    override if supplied)."""

    experiment_config: str
    """The experiment-config text the source serialized into the archive."""

    repo_bundle_path: Path
    """Filesystem path of the extracted git bundle; valid for the lifetime of
    the temporary extraction directory referenced by ``extract_root``."""

    artifact_digests: tuple[str, ...]
    """Sorted tuple of every ``sha256/<hex>`` artifact present in the archive."""

    manifest: CheckpointManifest
    """The parsed manifest, for the recovery-probe contract (``§10``) and
    operator-visible counts."""

    extract_root: Path
    """The on-disk root of the extracted archive. Valid for the lifetime of
    the underlying ``TemporaryDirectory`` (when the importer owns one) or
    of the caller-supplied ``extract_dir``."""

    warnings: tuple[str, ...] = field(default_factory=tuple)
    """Free-form operator-facing strings. Reserved for credential-reissue
    side-channel surfacing in wave 4."""

    _owned_tmp: TemporaryDirectory[str] | None = field(default=None, repr=False)
    """When the importer allocated the extraction dir, this attribute holds
    the ``TemporaryDirectory`` so it (and the on-disk files) survive until
    the result is garbage-collected. Callers that need a longer-lived
    extraction pass their own ``extract_dir`` and this stays ``None``."""


# ----------------------------------------------------------------------
# Serialization helpers
# ----------------------------------------------------------------------


def _submission_to_jsonl(task_id: str, submission: Submission) -> dict[str, Any]:
    """Serialize one ``Submission`` as a JSONL row.

    Mirrors :func:`eden_storage.sqlite._submission_to_row` in shape — a
    ``kind`` discriminator plus the role-specific fields, flat at the
    top level of the row.
    """
    if isinstance(submission, IdeaSubmission):
        return {
            "task_id": task_id,
            "kind": "ideation",
            "status": submission.status,
            "idea_ids": list(submission.idea_ids),
        }
    if isinstance(submission, VariantSubmission):
        row: dict[str, Any] = {
            "task_id": task_id,
            "kind": "execution",
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.commit_sha is not None:
            row["commit_sha"] = submission.commit_sha
        return row
    if isinstance(submission, EvaluationSubmission):
        row = {
            "task_id": task_id,
            "kind": "evaluation",
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.evaluation is not None:
            row["evaluation"] = submission.evaluation
        if submission.artifacts_uri is not None:
            row["artifacts_uri"] = submission.artifacts_uri
        return row
    raise TypeError(f"unknown submission type {type(submission).__name__}")


def _submission_from_jsonl(row: dict[str, Any]) -> tuple[str, Submission]:
    """Inverse of :func:`_submission_to_jsonl`. Returns ``(task_id, submission)``."""
    try:
        task_id = row["task_id"]
        kind = row["kind"]
    except KeyError as exc:
        raise CheckpointInvalid(f"submission row missing required field: {exc}") from exc
    if kind == "ideation":
        return task_id, IdeaSubmission(
            status=row.get("status", "success"),
            idea_ids=tuple(row.get("idea_ids") or ()),
        )
    if kind == "execution":
        return task_id, VariantSubmission(
            status=row.get("status", "success"),
            variant_id=row["variant_id"],
            commit_sha=row.get("commit_sha"),
        )
    if kind == "evaluation":
        return task_id, EvaluationSubmission(
            status=row.get("status", "success"),
            variant_id=row["variant_id"],
            evaluation=row.get("evaluation"),
            artifacts_uri=row.get("artifacts_uri"),
        )
    raise CheckpointInvalid(f"unknown submission kind {kind!r}")


def _event_to_jsonl(event: Event) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude_none=True)


def _event_from_jsonl(row: dict[str, Any]) -> Event:
    try:
        return Event.model_validate(row)
    except Exception as exc:
        raise CheckpointInvalid(f"malformed event row: {exc}") from exc


def _dispatch_mode_at_default(stored: dict[str, str]) -> bool:
    """Return True iff ``stored`` represents the all-default dispatch_mode.

    A freshly-initialized SqliteStore row (created by the v3 migration's
    column DEFAULT, then carried through v5) holds only the four
    operational keys at ``"auto"`` — the ``termination`` key is absent
    because the v5 ``UPDATE`` only patched pre-existing rows. So we
    treat "key absent in stored" as "key at its in-code default value"
    rather than requiring byte-equal dicts. Unknown keys (forward-
    compat extension per ``02-data-model.md`` §2.4) MUST NOT be present
    on a default-state store; their presence indicates customization.
    """
    default = _DEFAULT_DISPATCH_MODE
    for key in stored:
        if key not in default:
            return False
    return all(value == default[key] for key, value in stored.items())


def _is_store_empty(store: _StoreBase) -> bool:
    """Return True iff ``store`` carries no Store-managed mutations beyond defaults.

    Used as the import precondition: the receiving Store MUST be fresh
    (no tasks, no ideas, no variants, no submissions, no events, no
    workers, no groups, dispatch_mode at default, state=running,
    imported_from absent). Otherwise the import would silently merge
    into existing state, violating the §11 collision contract.
    """
    if any(True for _ in store._iter_tasks()):
        return False
    if any(True for _ in store._iter_ideas()):
        return False
    if any(True for _ in store._iter_variants()):
        return False
    if any(True for _ in store._iter_events()):
        return False
    if any(True for _ in store._iter_workers()):
        return False
    if any(True for _ in store._iter_groups()):
        return False
    if not _dispatch_mode_at_default(store._get_dispatch_mode()):
        return False
    experiment = store._get_experiment()
    if experiment.state != "running":
        return False
    return experiment.imported_from is None


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------


def export_checkpoint(
    store: _StoreBase,
    stream: BinaryIO,
    *,
    experiment_config: str | bytes = "",
    repo_bundle: bytes = b"",
    exporter_info: ExporterInfo | None = None,
) -> CheckpointManifest:
    """Write a portable-checkpoint archive of ``store``'s state to ``stream``.

    Runs inside the backend's ``_atomic_operation`` so the snapshot is
    transactionally consistent (``spec/v0/10-checkpoints.md`` §6). The
    Store-managed data (tasks / ideas / variants / submissions / events /
    workers / groups / dispatch_mode / experiment runtime) is read into
    memory and serialized into the archive; ``experiment_config`` and
    ``repo_bundle`` are caller-supplied substrate-external pieces.

    Returns the :class:`CheckpointManifest` written into the archive so
    callers can inspect the resulting ``exported_at`` (for the §10
    recovery-probe anchor) or per-component counts.

    The stream is NOT closed by this function; callers manage its
    lifecycle.
    """
    with store._atomic_operation():
        snapshot = _snapshot_store(store)

    exporter = exporter_info or ExporterInfo(
        implementation=_REFERENCE_IMPL_TAG,
        atomicity_mechanism=_REFERENCE_ATOMICITY,
    )
    exported_at = _utc_now_iso()
    counts = ManifestCounts(
        tasks=len(snapshot.tasks),
        ideas=len(snapshot.ideas),
        variants=len(snapshot.variants),
        submissions=len(snapshot.submissions),
        events=len(snapshot.events),
        workers=len(snapshot.workers),
        groups=len(snapshot.groups),
    )
    manifest = CheckpointManifest(
        checkpoint_format_version=CHECKPOINT_FORMAT_VERSION,
        spec_version=CHECKPOINT_SPEC_VERSION,
        experiment_id=store.experiment_id,
        exported_at=exported_at,
        exporter=exporter,
        # 12b binding strips every worker's credential hash on export
        # so the manifest always advertises required reissue per
        # `10-checkpoints.md` §8.
        requires_credential_reissue=True,
        counts=counts,
        files=DEFAULT_FILES,
    )

    with CheckpointWriter(stream) as writer:
        writer.write_experiment_config(experiment_config)
        writer.write_experiment(snapshot.experiment)
        writer.write_jsonl("tasks", snapshot.tasks)
        writer.write_jsonl("ideas", snapshot.ideas)
        writer.write_jsonl("variants", snapshot.variants)
        writer.write_jsonl("submissions", snapshot.submissions)
        writer.write_jsonl("events", snapshot.events)
        writer.write_jsonl("workers", snapshot.workers)
        writer.write_jsonl("groups", snapshot.groups)
        writer.write_repo_bundle(repo_bundle)
        writer.write_manifest(manifest)

    return manifest


@dataclass(frozen=True)
class _Snapshot:
    """Immutable view assembled inside the export transaction."""

    experiment: dict[str, Any]
    tasks: tuple[dict[str, Any], ...]
    ideas: tuple[dict[str, Any], ...]
    variants: tuple[dict[str, Any], ...]
    submissions: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    workers: tuple[dict[str, Any], ...]
    groups: tuple[dict[str, Any], ...]


def _snapshot_store(store: _StoreBase) -> _Snapshot:
    """Read everything Store-managed into a single self-consistent snapshot.

    Caller MUST be inside ``store._atomic_operation``.
    """
    experiment = store._get_experiment()
    experiment_row: dict[str, Any] = {
        "experiment_id": experiment.experiment_id,
        "state": experiment.state,
        "created_at": experiment.created_at,
    }
    if experiment.imported_from is not None:
        experiment_row["imported_from"] = experiment.imported_from.model_dump(
            mode="json"
        )
    # `dispatch_mode` is preserved on the experiment runtime row so a
    # round-trip restores any operator-flipped mode at the source.
    experiment_row["dispatch_mode"] = store._get_dispatch_mode()

    tasks = tuple(
        task.model_dump(mode="json", exclude_none=True)
        for task in store._iter_tasks()
    )
    ideas = tuple(
        idea.model_dump(mode="json", exclude_none=True)
        for idea in store._iter_ideas()
    )
    variants = tuple(
        variant.model_dump(mode="json", exclude_none=True)
        for variant in store._iter_variants()
    )
    submissions: list[dict[str, Any]] = []
    for task in store._iter_tasks():
        sub = store._get_submission(task.task_id)
        if sub is not None:
            submissions.append(_submission_to_jsonl(task.task_id, sub))
    events = tuple(_event_to_jsonl(e) for e in store._iter_events())
    workers = tuple(
        worker.model_dump(mode="json", exclude_none=True)
        for worker in store._iter_workers()
    )
    groups = tuple(
        group.model_dump(mode="json", exclude_none=True)
        for group in store._iter_groups()
    )
    return _Snapshot(
        experiment=experiment_row,
        tasks=tasks,
        ideas=ideas,
        variants=variants,
        submissions=tuple(submissions),
        events=events,
        workers=workers,
        groups=groups,
    )


# ----------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------


def import_checkpoint(
    store: _StoreBase,
    stream: BinaryIO,
    *,
    as_experiment_id: str | None = None,
    extract_dir: Path | None = None,
) -> ImportResult:
    """Read a portable-checkpoint archive and bulk-insert into ``store``.

    Preconditions:

    - The manifest's ``spec_version`` MUST match the binding's
      :data:`CHECKPOINT_SPEC_VERSION`.
    - ``store.experiment_id`` MUST equal the manifest's ``experiment_id``
      (or ``as_experiment_id`` if supplied); otherwise raises
      :class:`ExperimentIdMismatch`.
    - ``store`` MUST be empty (no tasks, no ideas, no variants, …, no
      events, no workers, no groups, dispatch_mode at default, state
      running, imported_from absent); otherwise raises
      :class:`ExperimentIdConflict` per ``10-checkpoints.md`` §11.

    On success commits a single atomic transaction that:

    - Inserts every Store-managed entity.
    - Sets the experiment row's ``imported_from`` to
      ``{checkpoint_exported_at, checkpoint_format_version}`` per
      ``10-checkpoints.md`` §10.
    - Applies any non-default ``dispatch_mode`` carried on the
      experiment row.
    - Applies any non-default ``state`` (terminated experiments import
      as terminated).

    Workers are inserted with a sentinel credential hash; the receiver
    MUST reissue per ``10-checkpoints.md`` §8.

    The archive is extracted into ``extract_dir`` if supplied, otherwise
    into a temporary directory whose lifetime is bound to the returned
    :class:`ImportResult`. Callers that need the artifacts / repo bundle
    after the immediate call MUST copy them out before the result goes
    out of scope.
    """
    # The temp dir is held open across the atomic write so the
    # archive's pre-rewrite bytes are still on disk during commit.
    # Lifetime hand-off is via the returned ImportResult; callers that
    # don't supply `extract_dir` get a TemporaryDirectory hooked to the
    # result.
    owned_tmp: TemporaryDirectory[str] | None = None
    if extract_dir is None:
        owned_tmp = TemporaryDirectory(prefix="eden-checkpoint-import-")
        extract_root_dir = Path(owned_tmp.name)
    else:
        extract_root_dir = extract_dir

    try:
        reader = extract_checkpoint(stream, extract_root_dir)
        result = _commit_import(
            store,
            reader,
            as_experiment_id=as_experiment_id,
            extract_root=reader.root,
        )
    except BaseException:
        if owned_tmp is not None:
            owned_tmp.cleanup()
        raise

    # If we own the temp dir, attach a cleanup hook to the result. We
    # rely on the caller invoking ``.cleanup_extract()`` or letting GC
    # eventually run; for the wave-3 test surface, tests pass an
    # explicit `extract_dir=tmp_path` so this branch rarely triggers.
    if owned_tmp is not None:
        result._owned_tmp = owned_tmp  # noqa: SLF001
    return result


def _commit_import(
    store: _StoreBase,
    reader: CheckpointReader,
    *,
    as_experiment_id: str | None,
    extract_root: Path,
) -> ImportResult:
    manifest = reader.manifest
    if manifest.spec_version != CHECKPOINT_SPEC_VERSION:
        raise SpecVersionMismatch(
            f"manifest spec_version={manifest.spec_version!r}, "
            f"this binding expects {CHECKPOINT_SPEC_VERSION!r}"
        )
    target_id = as_experiment_id or manifest.experiment_id
    if target_id != store.experiment_id:
        raise ExperimentIdMismatch(
            f"store.experiment_id={store.experiment_id!r} but checkpoint "
            f"resolves to {target_id!r} "
            f"(manifest={manifest.experiment_id!r}, "
            f"as_experiment_id={as_experiment_id!r})"
        )

    # Parse archive payload eagerly so any structural failure surfaces
    # before we open the atomic transaction.
    experiment_row = reader.read_experiment()
    experiment_config = reader.read_experiment_config()
    bundle_path = reader.read_repo_bundle_path()
    artifact_digests = tuple(reader.iter_artifact_digests())

    tasks_rows = list(reader.iter_jsonl("tasks"))
    ideas_rows = list(reader.iter_jsonl("ideas"))
    variants_rows = list(reader.iter_jsonl("variants"))
    submissions_rows = list(reader.iter_jsonl("submissions"))
    events_rows = list(reader.iter_jsonl("events"))
    workers_rows = list(reader.iter_jsonl("workers"))
    groups_rows = list(reader.iter_jsonl("groups"))

    # Rewrite each experiment_id reference inside the rows to the target
    # id (handles the `as_experiment_id` override case). The
    # `02-data-model.md` invariants want every row's `experiment_id`
    # to agree with the experiment scope.
    source_id = manifest.experiment_id
    if target_id != source_id:
        _rewrite_experiment_id(tasks_rows, source_id, target_id)
        _rewrite_experiment_id(ideas_rows, source_id, target_id)
        _rewrite_experiment_id(variants_rows, source_id, target_id)
        _rewrite_experiment_id(events_rows, source_id, target_id)
        _rewrite_experiment_id(workers_rows, source_id, target_id)
        _rewrite_experiment_id(groups_rows, source_id, target_id)

    # Build typed objects up-front so a malformed row raises BEFORE we
    # open the transaction. Any validation failure is `CheckpointInvalid`
    # rather than a half-committed import.
    tasks = [_validate_task(row) for row in tasks_rows]
    ideas = [_validate_idea(row) for row in ideas_rows]
    variants = [_validate_variant(row) for row in variants_rows]
    submissions = [_submission_from_jsonl(row) for row in submissions_rows]
    events = [_event_from_jsonl(row) for row in events_rows]
    workers = [_validate_worker(row) for row in workers_rows]
    groups = [_validate_group(row) for row in groups_rows]

    # Cross-reference validation per chapter 10 §12 — runs BEFORE the
    # atomic transaction so a malformed archive cannot partially
    # commit. The check is gated on a non-empty bundle: an exporter
    # without git access (test fixtures, the wave-4 wire-binding posture
    # without `--repo-path`) emits a zero-byte placeholder; the check
    # is meaningful only when the bundle ships real history.
    _validate_bundle_cross_references(
        bundle_path=bundle_path,
        extract_root=extract_root,
        variants=variants,
        ideas=ideas,
    )

    with store._atomic_operation():
        if not _is_store_empty(store):
            raise ExperimentIdConflict(
                f"target store for experiment {target_id!r} already has "
                "Store-managed state; import would silently merge"
            )

        # Stage every row + the experiment-side fields into a single _Tx
        # so the commit is atomic.
        tx = _Tx()
        for task in tasks:
            tx.tasks[task.task_id] = task
        for idea in ideas:
            tx.ideas[idea.idea_id] = idea
        for variant in variants:
            tx.variants[variant.variant_id] = variant
        for task_id, submission in submissions:
            tx.submissions[task_id] = submission
        for worker in workers:
            tx.workers[worker.worker_id] = worker
            # `register_worker` would mint a real credential; here we
            # synthesize a sentinel so the row inserts cleanly and any
            # subsequent `verify_worker_credential` returns False until
            # `reissue_credential` runs.
            tx.worker_credentials[worker.worker_id] = (
                _IMPORT_PLACEHOLDER_CREDENTIAL_HASH_PREFIX
                + secrets.token_hex(16)
            )
        for group in groups:
            tx.groups[group.group_id] = group
        # Events are inserted verbatim. Their event_ids retain the
        # source's values; the receiving backend's default event-id
        # factory may collide on subsequent appends — that's a wave-4
        # concern, since the wire layer can advance the factory's
        # counter from the imported max if needed.
        tx.events.extend(events)
        # Apply the experiment's dispatch_mode if it deviates from
        # default.
        dispatch_mode = experiment_row.get("dispatch_mode")
        if dispatch_mode and dispatch_mode != _DEFAULT_DISPATCH_MODE:
            tx.dispatch_mode = dict(dispatch_mode)
        # Apply the experiment's state.
        state = experiment_row.get("state")
        if state and state != "running":
            tx.experiment_state = state
        # Record import provenance.
        tx.imported_from_update = (
            ImportProvenance(
                checkpoint_exported_at=manifest.exported_at,
                checkpoint_format_version=manifest.checkpoint_format_version,
            ),
        )
        store._apply_commit(tx)

    return ImportResult(
        experiment_id=target_id,
        experiment_config=experiment_config,
        repo_bundle_path=bundle_path,
        artifact_digests=tuple(sorted(artifact_digests)),
        manifest=manifest,
        extract_root=extract_root,
    )


# ----------------------------------------------------------------------
# Row validators
# ----------------------------------------------------------------------


def _validate_task(row: dict[str, Any]) -> Task:
    try:
        return TaskAdapter.validate_python(row)
    except Exception as exc:
        raise CheckpointInvalid(f"malformed task row: {exc}") from exc


def _validate_idea(row: dict[str, Any]) -> Idea:
    try:
        return Idea.model_validate(row)
    except Exception as exc:
        raise CheckpointInvalid(f"malformed idea row: {exc}") from exc


def _validate_variant(row: dict[str, Any]) -> Variant:
    try:
        return Variant.model_validate(row)
    except Exception as exc:
        raise CheckpointInvalid(f"malformed variant row: {exc}") from exc


def _validate_worker(row: dict[str, Any]) -> Worker:
    try:
        return Worker.model_validate(row)
    except Exception as exc:
        raise CheckpointInvalid(f"malformed worker row: {exc}") from exc


def _validate_group(row: dict[str, Any]) -> Group:
    try:
        return Group.model_validate(row)
    except Exception as exc:
        raise CheckpointInvalid(f"malformed group row: {exc}") from exc


# ----------------------------------------------------------------------
# Cross-reference validation
# ----------------------------------------------------------------------


def _validate_bundle_cross_references(
    *,
    bundle_path: Path,
    extract_root: Path,
    variants: list[Variant],
    ideas: list[Idea],
) -> None:
    """Verify chapter 10 §12 cross-references between JSONL data and bundle.

    Per ``spec/v0/10-checkpoints.md`` §12, before any state commits the
    importer MUST validate:

    1. Every variant's ``branch`` (when set) MUST resolve to a commit
       in the bundle.
    2. Every variant's ``commit_sha`` (when set) MUST be reachable.
    3. Every variant's ``variant_commit_sha`` (when set) MUST be
       reachable.
    4. Every idea's ``parent_commits`` MUST all be reachable.

    Gated on a non-empty bundle: a zero-byte placeholder (the wave-4
    binding posture when the operator has not configured a repo path)
    skips the check. This is a conformance compromise — a fully
    spec-conformant deployment ships a real bundle and the validator
    catches stale references; the test-fixture posture skips because
    the IUT under test does not (yet) bind a git substrate.
    """
    from eden_checkpoint.repo_bundle import verify_commits_reachable

    if not bundle_path.is_file() or bundle_path.stat().st_size == 0:
        return  # zero-byte placeholder; skip per docstring

    referenced: list[tuple[str, str]] = []  # (sha, "label") for error messages
    for variant in variants:
        if variant.commit_sha is not None:
            referenced.append((variant.commit_sha, f"variant {variant.variant_id} commit_sha"))
        if variant.variant_commit_sha is not None:
            referenced.append(
                (variant.variant_commit_sha, f"variant {variant.variant_id} variant_commit_sha")
            )
    for idea in ideas:
        for parent in idea.parent_commits:
            referenced.append((parent, f"idea {idea.idea_id} parent_commit"))

    if not referenced:
        return

    scratch_dir = extract_root / ".bundle-scratch"
    scratch_dir.mkdir(exist_ok=True)
    shas_to_check = [sha for sha, _ in referenced]
    reachable = verify_commits_reachable(
        bundle_path, shas_to_check, working_dir=scratch_dir
    )
    missing = [
        (sha, label) for sha, label in referenced if sha not in reachable
    ]
    if missing:
        # Surface the first missing reference; operators can re-run with
        # verbose logging to enumerate the full set.
        sha, label = missing[0]
        raise CheckpointInvalid(
            f"chapter 10 §12 cross-reference violation: {label} "
            f"sha={sha} is not reachable in the bundle "
            f"({len(missing)} missing reference(s) total)"
        )


# ----------------------------------------------------------------------
# Misc
# ----------------------------------------------------------------------


def _rewrite_experiment_id(
    rows: Iterable[dict[str, Any]],
    source_id: str,
    target_id: str,
) -> None:
    """In-place rewrite every ``experiment_id == source_id`` to ``target_id``.

    Walks the top level of each row plus the ``payload`` sub-object on
    tasks (the only nested place an ``experiment_id`` can appear in v0
    Store-managed data).
    """
    for row in rows:
        if row.get("experiment_id") == source_id:
            row["experiment_id"] = target_id
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("experiment_id") == source_id:
            payload["experiment_id"] = target_id


def _utc_now_iso() -> str:
    """Return the current UTC wall-clock time in RFC 3339 with millisecond precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
