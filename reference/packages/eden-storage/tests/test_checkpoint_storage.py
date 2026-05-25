"""Wave-3 portable-checkpoint export / import: parametrized backend round-trip.

Mirrors the wave-2 ``test_checkpoint_roundtrip.py`` test (which exercises
the format library directly) at the Store layer: every test runs once
per backend (memory / sqlite / postgres) via ``make_store``.

Coverage matches the wave-3 plan:

- Empty checkpoint round-trip.
- Full-experiment round-trip (tasks + ideas + variants + submissions + events).
- Terminated-experiment round-trip preserves state.
- Worker + group portability.
- Recovery-probe idempotency (second import → ``ExperimentIdConflict``).
- ``ExperimentIdMismatch`` when manifest id ≠ store id and no override.
- ``as_experiment_id`` override succeeds.
- Spec-version mismatch rejection.
- Cross-backend interop (SQLite ↔ InMemory) — separate test that
  doesn't use the parametrized fixture.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from eden_checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointInvalid,
    CheckpointManifest,
    CheckpointWriter,
    ExperimentIdConflict,
    ExperimentIdMismatch,
    ManifestCounts,
    SpecVersionMismatch,
)
from eden_contracts import (
    Idea,
    Variant,
)
from eden_storage import (
    InMemoryStore,
    SqliteStore,
    Store,
    VariantSubmission,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ready_idea(store: Store, idea_id: str) -> None:
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug=f"feat-{idea_id}",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri=f"file:///artifacts/{idea_id}",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_idea_ready(idea_id)


def _populate_full_experiment(store: Store) -> None:
    """Drive ``store`` through a small but cross-product-shaped scenario.

    Produces: 1 idea (drafting → ready → dispatched), 1 variant
    (starting → success → integrated), an ideation + execution task pair
    (the execution task in submitted state after the executor wrote
    ``commit_sha``), and the corresponding event stream.
    """
    store.register_worker("checkpoint-ideator")
    store.register_worker("checkpoint-executor")
    store.register_group("orchestrators", members=())
    _ready_idea(store, "idea-x")
    store.create_variant(
        Variant(
            variant_id="var-x",
            experiment_id=store.experiment_id,
            idea_id="idea-x",
            status="starting",
            parent_commits=["a" * 40],
            branch="work/feat-idea-x-var-x",
            started_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.create_execution_task("t-exec", "idea-x")
    claim = store.claim("t-exec", "checkpoint-executor")
    store.submit(
        "t-exec",
        claim.worker_id,
        VariantSubmission(status="success", variant_id="var-x", commit_sha="b" * 40),
    )


def _make_manifest(
    experiment_id: str,
    *,
    counts: dict[str, int] | None = None,
    spec_version: str = CHECKPOINT_SPEC_VERSION,
    checkpoint_format_version: str = CHECKPOINT_FORMAT_VERSION,
) -> CheckpointManifest:
    full = {
        "tasks": 0, "ideas": 0, "variants": 0, "submissions": 0,
        "events": 0, "workers": 0, "groups": 0,
    }
    if counts:
        full.update(counts)
    return CheckpointManifest.model_validate(
        {
            "checkpoint_format_version": checkpoint_format_version,
            "spec_version": spec_version,
            "experiment_id": experiment_id,
            "exported_at": "2026-05-06T15:00:00Z",
            "requires_credential_reissue": True,
            "counts": ManifestCounts(**full).model_dump(),
            "files": DEFAULT_FILES.model_dump(),
        }
    )


def _write_scaffold_archive(stream: io.BytesIO, manifest: CheckpointManifest) -> None:
    with CheckpointWriter(stream) as w:
        w.write_experiment_config("parallel_variants: 1\n")
        w.write_experiment(
            {
                "experiment_id": manifest.experiment_id,
                "state": "running",
                "created_at": "2026-04-23T00:00:00Z",
            }
        )
        for k in ("tasks", "ideas", "variants", "submissions", "events", "workers", "groups"):
            w.write_jsonl(k, [])
        w.write_repo_bundle(b"")
        w.write_manifest(manifest)


# ----------------------------------------------------------------------
# Tests parametrized across all three backends
# ----------------------------------------------------------------------


def test_empty_round_trip(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """A store with no Store-managed mutations exports + reimports cleanly."""
    source = make_store("exp-empty", seed_workers=False)
    archive = io.BytesIO()
    manifest = source.export_checkpoint(
        archive, experiment_config="parallel_variants: 1\n"
    )
    assert manifest.experiment_id == "exp-empty"
    assert manifest.requires_credential_reissue is True
    assert manifest.counts.tasks == 0

    target = make_store("exp-empty", seed_workers=False)
    archive.seek(0)
    result = target.import_checkpoint(archive, extract_dir=tmp_path)
    assert result.experiment_id == "exp-empty"
    assert target.read_experiment().imported_from is not None


def test_full_experiment_round_trip(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Tasks, ideas, variants, submissions, events round-trip verbatim."""
    source = make_store("exp-full", seed_workers=False)
    _populate_full_experiment(source)

    src_tasks = sorted(t.task_id for t in source.list_tasks())
    src_ideas = sorted(i.idea_id for i in source.list_ideas())
    src_variants = sorted(v.variant_id for v in source.list_variants())
    src_events = [(e.type, e.experiment_id) for e in source.events()]
    src_workers = sorted(w.worker_id for w in source.list_workers())
    src_groups = sorted(g.group_id for g in source.list_groups())

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-full", seed_workers=False)
    archive.seek(0)
    result = target.import_checkpoint(archive, extract_dir=tmp_path)

    assert result.experiment_id == "exp-full"
    assert sorted(t.task_id for t in target.list_tasks()) == src_tasks
    assert sorted(i.idea_id for i in target.list_ideas()) == src_ideas
    assert sorted(v.variant_id for v in target.list_variants()) == src_variants
    assert [(e.type, e.experiment_id) for e in target.events()] == src_events
    assert sorted(w.worker_id for w in target.list_workers()) == src_workers
    assert sorted(g.group_id for g in target.list_groups()) == src_groups

    # The submission on the executed task survives.
    assert target.read_submission("t-exec") is not None


def test_terminated_round_trip(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """A terminated source experiment imports as terminated."""
    source = make_store("exp-term", seed_workers=False)
    source.register_worker("admin-worker")
    source.register_group("admins", members=["admin-worker"])
    source.terminate_experiment(reason="done", terminated_by="admin-worker")
    assert source.read_experiment().state == "terminated"

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-term", seed_workers=False)
    archive.seek(0)
    target.import_checkpoint(archive, extract_dir=tmp_path)
    assert target.read_experiment().state == "terminated"


def test_worker_and_group_portability(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Workers + groups + memberships survive round-trip."""
    source = make_store("exp-wg", seed_workers=False)
    source.register_worker("alice", labels={"role": "executor"})
    source.register_worker("bob")
    source.register_group("orchestrators", members=["alice", "bob"])
    source.register_group("admins", members=["alice"])

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-wg", seed_workers=False)
    archive.seek(0)
    target.import_checkpoint(archive, extract_dir=tmp_path)

    assert {w.worker_id for w in target.list_workers()} == {"alice", "bob"}
    assert target.read_worker("alice").labels == {"role": "executor"}
    assert target.resolve_worker_in_group("alice", "admins") is True
    assert target.resolve_worker_in_group("bob", "admins") is False
    assert target.resolve_worker_in_group("alice", "orchestrators") is True


def test_recovery_probe_imported_from_set(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """After import, ``read_experiment().imported_from`` matches the source manifest."""
    source = make_store("exp-rp", seed_workers=False)
    archive = io.BytesIO()
    manifest = source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-rp", seed_workers=False)
    archive.seek(0)
    target.import_checkpoint(archive, extract_dir=tmp_path)

    experiment = target.read_experiment()
    assert experiment.imported_from is not None
    assert experiment.imported_from.checkpoint_exported_at == manifest.exported_at
    assert (
        experiment.imported_from.checkpoint_format_version
        == manifest.checkpoint_format_version
    )


def test_native_experiment_imported_from_is_none(
    make_store: Callable[..., Store],
) -> None:
    """A natively-created experiment has ``imported_from is None``."""
    store = make_store("exp-native", seed_workers=False)
    assert store.read_experiment().imported_from is None


def test_double_import_raises_conflict(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Importing into an already-populated store raises ``ExperimentIdConflict``."""
    source = make_store("exp-conflict", seed_workers=False)
    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-conflict", seed_workers=False)
    archive.seek(0)
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    target.import_checkpoint(archive, extract_dir=first_dir)

    archive.seek(0)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    with pytest.raises(ExperimentIdConflict):
        target.import_checkpoint(archive, extract_dir=second_dir)


def test_experiment_id_mismatch_without_override(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Manifest id ≠ store id with no override → ``ExperimentIdMismatch``."""
    source = make_store("exp-source", seed_workers=False)
    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-target", seed_workers=False)
    archive.seek(0)
    with pytest.raises(ExperimentIdMismatch):
        target.import_checkpoint(archive, extract_dir=tmp_path)


def test_as_experiment_id_override_succeeds(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """``as_experiment_id`` rewrites the manifest's id; the import succeeds."""
    source = make_store("exp-renamed-source", seed_workers=False)
    _populate_full_experiment(source)
    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-renamed-target", seed_workers=False)
    archive.seek(0)
    target.import_checkpoint(
        archive,
        as_experiment_id="exp-renamed-target",
        extract_dir=tmp_path,
    )

    # Every row's experiment_id has been rewritten to the target's id.
    assert all(
        t.payload.experiment_id == "exp-renamed-target"
        for t in target.list_tasks()
        if t.kind == "ideation"
    )
    assert all(i.experiment_id == "exp-renamed-target" for i in target.list_ideas())
    assert all(v.experiment_id == "exp-renamed-target" for v in target.list_variants())
    assert all(
        e.experiment_id == "exp-renamed-target" for e in target.events()
    )
    assert all(w.experiment_id == "exp-renamed-target" for w in target.list_workers())
    assert all(g.experiment_id == "exp-renamed-target" for g in target.list_groups())


def test_spec_version_mismatch_rejected(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """A manifest with a different ``spec_version`` raises ``SpecVersionMismatch``."""
    stream = io.BytesIO()
    bad_manifest = _make_manifest("exp-mismatch", spec_version="v99")
    _write_scaffold_archive(stream, bad_manifest)

    target = make_store("exp-mismatch", seed_workers=False)
    stream.seek(0)
    with pytest.raises(SpecVersionMismatch):
        target.import_checkpoint(stream, extract_dir=tmp_path)

    # State remained empty — no partial commit.
    assert target.read_experiment().imported_from is None


def test_corrupt_archive_rejected(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Non-tar bytes → ``CheckpointInvalid``; no state mutated."""
    target = make_store("exp-corrupt", seed_workers=False)
    with pytest.raises(CheckpointInvalid):
        target.import_checkpoint(io.BytesIO(b"not a tar"), extract_dir=tmp_path)
    assert target.read_experiment().imported_from is None


def test_workers_credentials_reissued_on_import(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Imported workers get freshly-minted credentials atomically with the
    import (``10-checkpoints.md`` §8 step 4); the source's plaintext is
    gone, and the new tokens surface on ``ImportResult.reissued_credentials``.
    """
    source = make_store("exp-creds", seed_workers=False)
    _, source_token_a = source.register_worker("worker-a")
    _, source_token_b = source.register_worker("worker-b")
    assert source_token_a is not None
    assert source_token_b is not None

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-creds", seed_workers=False)
    archive.seek(0)
    result = target.import_checkpoint(archive, extract_dir=tmp_path)

    # The source's plaintext token MUST NOT authenticate against the
    # imported store (per chapter 10 §8: receiver mints fresh creds).
    assert target.verify_worker_credential("worker-a", source_token_a) is False
    assert target.verify_worker_credential("worker-b", source_token_b) is False

    # Every imported worker carries a fresh token surfaced on the
    # ImportResult, and each token authenticates against the imported
    # store — the auto-reissue is atomic with the rest of the import.
    assert set(result.reissued_credentials) == {"worker-a", "worker-b"}
    new_token_a = result.reissued_credentials["worker-a"]
    new_token_b = result.reissued_credentials["worker-b"]
    assert new_token_a != source_token_a
    assert new_token_b != source_token_b
    assert target.verify_worker_credential("worker-a", new_token_a) is True
    assert target.verify_worker_credential("worker-b", new_token_b) is True


def test_empty_workers_yields_empty_reissued_credentials(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """A checkpoint with no workers produces an empty reissued_credentials map."""
    source = make_store("exp-no-workers", seed_workers=False)
    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-no-workers", seed_workers=False)
    archive.seek(0)
    result = target.import_checkpoint(archive, extract_dir=tmp_path)
    assert dict(result.reissued_credentials) == {}


# ----------------------------------------------------------------------
# Cross-backend interop (not parametrized; explicit pairs)
# ----------------------------------------------------------------------


def test_sqlite_to_memory_round_trip(tmp_path: Path) -> None:
    """An archive emitted by SqliteStore is consumable by InMemoryStore."""
    db_path = tmp_path / "source.db"
    source = SqliteStore("exp-cross", db_path)
    _populate_full_experiment(source)

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")
    source.close()

    target = InMemoryStore("exp-cross")
    archive.seek(0)
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    target.import_checkpoint(archive, extract_dir=extract_dir)

    assert {t.task_id for t in target.list_tasks()} == {"t-exec"}
    assert {i.idea_id for i in target.list_ideas()} == {"idea-x"}
    assert {v.variant_id for v in target.list_variants()} == {"var-x"}


def test_memory_to_sqlite_round_trip(tmp_path: Path) -> None:
    """An archive emitted by InMemoryStore is consumable by SqliteStore."""
    source = InMemoryStore("exp-cross2")
    _populate_full_experiment(source)

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    db_path = tmp_path / "target.db"
    target = SqliteStore("exp-cross2", db_path)
    archive.seek(0)
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    target.import_checkpoint(archive, extract_dir=extract_dir)

    assert {t.task_id for t in target.list_tasks()} == {"t-exec"}
    assert {i.idea_id for i in target.list_ideas()} == {"idea-x"}
    assert {v.variant_id for v in target.list_variants()} == {"var-x"}
    target.close()


# ----------------------------------------------------------------------
# Manifest counts
# ----------------------------------------------------------------------


def test_manifest_counts_match_payload(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """The emitted manifest's counts equal the actual JSONL row counts."""
    source = make_store("exp-counts", seed_workers=False)
    _populate_full_experiment(source)

    archive = io.BytesIO()
    manifest = source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-counts", seed_workers=False)
    archive.seek(0)
    target.import_checkpoint(archive, extract_dir=tmp_path)

    assert manifest.counts.tasks == len(target.list_tasks())
    assert manifest.counts.ideas == len(target.list_ideas())
    assert manifest.counts.variants == len(target.list_variants())
    assert manifest.counts.events == len(target.events())
    assert manifest.counts.workers == len(target.list_workers())
    assert manifest.counts.groups == len(target.list_groups())


def test_event_counter_reseeded_after_import(
    make_store: Callable[..., Store], tmp_path: Path
) -> None:
    """Codex round-1 #3: import advances the live event-id counter past imported events.

    Before this fix, the receiver's default `_event_ids` counter
    restarted at 1 every construction; imported events with
    `evt-NNNNNN` ids (the default factory format) collided with the
    next emitted event's id on the UNIQUE constraint. The reseed
    in `_commit_import` advances the counter to `max(imported) + 1`.

    Regression test: populate the sender with several events
    (driving an ideation task generates `task.created` etc.), export,
    import into a fresh receiver, then emit ONE new event on the
    receiver. The new event's id MUST exceed every imported id.
    """
    source = make_store("exp-evtreseed", seed_workers=False)
    _populate_full_experiment(source)
    pre_export_events = source.events()
    assert len(pre_export_events) >= 1
    # Note: source events use evt-NNNNNN format under the default
    # factory; verify before we rely on that in the assertion.
    last_imported_id = pre_export_events[-1].event_id
    assert last_imported_id.startswith("evt-")
    last_n = int(last_imported_id.removeprefix("evt-"))

    archive = io.BytesIO()
    source.export_checkpoint(archive, experiment_config="x")

    target = make_store("exp-evtreseed", seed_workers=False)
    archive.seek(0)
    target.import_checkpoint(archive, extract_dir=tmp_path)

    # Drive one more event on the receiver. We use create_ideation_task
    # because that emits a `task.created` event via the default factory.
    target.create_ideation_task("post-import-evt")
    receiver_events = target.events()
    new_event = receiver_events[-1]
    new_n = int(new_event.event_id.removeprefix("evt-"))
    assert new_n > last_n, (last_imported_id, new_event.event_id)


# Hint for ruff that `Any` is intentionally imported for the test
# fixture annotation surface; silences F401 in case ruff reorders.
_: Any = None
