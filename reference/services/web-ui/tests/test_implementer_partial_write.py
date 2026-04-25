"""Partial-write recovery tests for the implementer module.

Pins each branch of §C-recovery from the Phase 9c plan:

- Phase 2 failure (create_ref raises): orphan, then sweeper recovers.
- Phase 3 transport-only sub-case A (server committed, still
  ``submitted``): success page via read-back.
- Phase 3 transport-only sub-case A' (server committed, terminalized
  to ``failed``): success page via read-back.
- Phase 3 transport-only sub-case B (never committed): orphan
  (``auto`` recovery).
- Phase 3 ``WrongToken`` short-circuit: orphan (``auto``).
- Phase 3 ``ConflictingResubmission`` short-circuit: orphan
  (``conflict``).
- Pre-Phase-1 ref-collision guard: form re-render, no create_trial.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from conftest import (
    get_csrf,
    make_child_commit,
    seed_implement_task,
)
from eden_dispatch import sweep_expired_claims
from eden_git import GitRepo
from eden_storage import (
    ConflictingResubmission,
    IllegalTransition,
    ImplementSubmission,
    InMemoryStore,
    WrongToken,
)
from eden_web_ui.routes import implementer as implementer_routes
from fastapi.testclient import TestClient


def _post_form(client: TestClient, url: str, fields: list[tuple[str, str]]):
    body = urlencode(fields)
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


@pytest.fixture(autouse=True)
def _clear_claims():
    implementer_routes._CLAIMS.clear()
    yield
    implementer_routes._CLAIMS.clear()


def _claim_and_prep(
    client: TestClient,
    store: InMemoryStore,
    bare_repo: GitRepo,
    base_sha: str,
    slug: str = "demo",
) -> tuple[str, str]:
    """Seed task, claim, and produce a child commit. Returns (task_id, child_sha)."""
    task_id, _ = seed_implement_task(store, base_sha=base_sha, slug=slug)
    csrf = get_csrf(client)
    resp = _post_form(
        client, f"/implementer/{task_id}/claim", [("csrf_token", csrf)]
    )
    assert resp.status_code == 303
    child_sha = make_child_commit(bare_repo, base_sha, slug)
    return task_id, child_sha


class TestPhase2Failure:
    def test_create_ref_failure_renders_orphan_then_sweeper_recovers(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="ph2"
        )
        # Replace `create_ref` on this repo instance only.
        def boom(self, refname, sha):  # noqa: ARG001
            raise RuntimeError("fs full")

        monkeypatch.setattr(GitRepo, "create_ref", boom)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "repo error" in resp.text
        assert "auto-recovers" in resp.text.lower()
        # The orphan-page prose must match the actual state: task
        # is still claimed by this session until the TTL expires.
        assert "still be claimed by this session" in resp.text
        # Trial in starting; task still claimed.
        trials = store.list_trials()
        assert len(trials) == 1
        assert trials[0].status == "starting"
        assert store.read_task(task_id).state == "claimed"
        assert bare_repo.list_refs("refs/heads/work/*") == []
        # Sweeper recovers: trial → error, task → pending.
        future = datetime(2026, 4, 24, 13, 0, 1, tzinfo=UTC) + timedelta(hours=2)
        reclaimed = sweep_expired_claims(store, now=future)
        assert reclaimed == 1
        assert store.read_task(task_id).state == "pending"
        assert store.list_trials()[0].status == "error"


class TestPhase3TransportOnly:
    def test_subcase_A_committed_still_submitted(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="suba"
        )
        original = store.submit
        # All attempts commit (idempotently after the first) but ALSO
        # raise — simulating "server processed it, response was lost
        # in transit." Forces the route to drain its retries and then
        # reach the read-back branch, which should observe
        # state == "submitted" with our equivalent submission and
        # render the success page.
        def commit_then_raise(task_id, token, submission):
            original(task_id, token, submission)
            raise RuntimeError("network glitch")

        monkeypatch.setattr(store, "submit", commit_then_raise)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 200
        assert child_sha in resp.text
        assert store.read_task(task_id).state == "submitted"

    def test_subcase_A_prime_committed_terminalized_to_failed(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="apr"
        )
        original = store.submit
        terminalized = {"done": False}
        # First call commits and raises; the orchestrator then rejects
        # before any retry runs (simulating very fast acceptance/rejection
        # turnaround). Subsequent calls raise transport errors.
        def commit_terminalize_raise(task_id, token, submission):
            if not terminalized["done"]:
                original(task_id, token, submission)
                store.reject(task_id, "worker_error")
                terminalized["done"] = True
            raise RuntimeError("net")

        monkeypatch.setattr(store, "submit", commit_terminalize_raise)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 200
        assert child_sha in resp.text
        assert store.read_task(task_id).state == "failed"

    def test_subcase_B_never_committed(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="subb"
        )

        def always_fail(task_id, token, submission):  # noqa: ARG001
            raise RuntimeError("net")

        monkeypatch.setattr(store, "submit", always_fail)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "auto-recovers" in resp.text.lower()
        # Sub-case B: claim is still live until TTL expiry.
        assert "still be claimed by this session" in resp.text
        # Trial in starting, task still claimed, ref present.
        assert store.read_task(task_id).state == "claimed"
        trials = store.list_trials()
        assert len(trials) == 1
        assert trials[0].status == "starting"
        assert bare_repo.list_refs("refs/heads/work/*") != []


class TestPhase3DefinitiveErrors:
    def test_wrong_token_short_circuits_to_orphan(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="wt"
        )
        attempts = {"n": 0}

        def fake_submit(task_id, token, submission):  # noqa: ARG001
            attempts["n"] += 1
            raise WrongToken("expired")

        monkeypatch.setattr(store, "submit", fake_submit)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "wrong-token" in resp.text
        # No retry on definitive errors.
        assert attempts["n"] == 1

    def test_conflicting_resubmission_renders_conflict_orphan(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="cr"
        )

        def fake_submit(task_id, token, submission):  # noqa: ARG001
            raise ConflictingResubmission("differs")

        monkeypatch.setattr(store, "submit", fake_submit)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "conflicting-resubmission" in resp.text
        assert "operator intervention may be needed" in resp.text


class TestRefCollisionGuard:
    def test_pre_phase_1_collision_renders_form_error_no_trial(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha, slug="coll")
        csrf = get_csrf(signed_in_impl_client)
        # Claim to populate _CLAIMS with a known trial_id.
        _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        # Find the trial_id we got assigned.
        keys = list(implementer_routes._CLAIMS.keys())
        assert len(keys) == 1
        _, trial_id = implementer_routes._CLAIMS[keys[0]]
        # Pre-create the ref the route would have written so the
        # guard fires.
        child_sha = make_child_commit(bare_repo, base_sha, "coll-tip")
        bare_repo.create_ref(f"refs/heads/work/coll-{trial_id}", child_sha)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 400
        assert "already exists" in resp.text
        # No trial was created (guard runs Pre-Phase-1).
        assert store.list_trials() == []
        # Claim entry retained; the user can retry.
        assert keys[0] in implementer_routes._CLAIMS


class TestPhase1TransportFailure:
    def test_create_trial_transport_failure_renders_indeterminate_orphan(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="ph1"
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("transport")

        monkeypatch.setattr(store, "create_trial", boom)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "create_trial transport failure" in resp.text
        assert "indeterminate" in resp.text.lower()


class TestReadbackProbeFailure:
    def test_read_task_failure_renders_transport_orphan(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="rb"
        )

        # All submit attempts raise transport errors.
        def always_raise(*_args, **_kwargs):
            raise RuntimeError("net")

        monkeypatch.setattr(store, "submit", always_raise)
        # The submit-catch path uses store.read_task to decide between
        # auto/conflict/transport. If read_task itself transport-fails,
        # the outcome is genuinely indeterminate; the route should not
        # claim "auto-recovers."
        original_read_task = store.read_task
        calls = {"n": 0}

        def read_task_fail(task_id):
            # Let the route's earlier read_task call (in the submit
            # handler, before retries) succeed; only fail when called
            # from the read-back probe.
            if calls["n"] > 0:
                raise RuntimeError("read-back outage")
            calls["n"] += 1
            return original_read_task(task_id)

        monkeypatch.setattr(store, "read_task", read_task_fail)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "read-back failed" in resp.text
        # The orphan page must NOT promise auto-recovery in this branch.
        assert "auto-recovers" not in resp.text.lower()


class TestStoreInvariantViolation:
    def test_read_submission_none_renders_transport_orphan(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        """Read-back observes terminal state with no recorded submission.

        That is implementation-illegal in the reference store; the
        UI must render the transport-flavored orphan page rather
        than a misleading conflict banner.
        """
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="inv"
        )
        original_submit = store.submit
        # Simulate "server committed first attempt, then transport
        # failures forever" by committing once and then raising; retry
        # logic should swallow the raise and run read-back. read_task
        # is left intact (state=submitted), but read_submission is
        # patched to return None to simulate the illegal state.
        committed = {"done": False}

        def fake_submit(task_id, token, submission):
            if not committed["done"]:
                original_submit(task_id, token, submission)
                committed["done"] = True
            raise RuntimeError("net")

        monkeypatch.setattr(store, "submit", fake_submit)

        def fake_read_submission(task_id):  # noqa: ARG001
            return None

        monkeypatch.setattr(store, "read_submission", fake_read_submission)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "store invariant violation" in resp.text


def _build_submission(child_sha: str) -> ImplementSubmission:
    """Helper used by future tests; kept for readability of the submission shape."""
    return ImplementSubmission(
        status="success", trial_id="trial-x", commit_sha=child_sha
    )


class TestPhase3IllegalTransitionReadback:
    """`IllegalTransition` feeds read-back, not a definitive short-circuit.

    These three sub-cases cover the §K-2 fix to chunk 9c: the
    chunk-9c implementer module originally treated
    ``IllegalTransition`` as a definitive auto-orphan, which
    mis-classifies the "we won, response lost, orchestrator
    already terminalized" sequence. The fix routes
    ``IllegalTransition`` to read-back; the read-back's three
    branches (state==pending, state==completed with equivalent
    prior, state==completed with non-equivalent prior) cover the
    actual outcomes.
    """

    def test_pending_after_illegal_transition_renders_auto_orphan(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="ita"
        )
        # Reclaim the task ourselves so its state is pending; the
        # route's submit will then raise IllegalTransition. Read-back
        # finds state==pending → orphan auto.
        store.reclaim(task_id, "operator")
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        assert resp.status_code == 502
        assert "auto-recovers" in resp.text
        # Banner mentions reclaim, distinguishing this from "we won"
        # (sub-case b below) which renders the success page.
        assert "task reclaimed" in resp.text

    def test_completed_with_equivalent_prior_renders_success(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        """The lens that flips the chunk-9c short-circuit bug to correct.

        Models "our first submit attempt committed, transport lost
        the response, the orchestrator already terminalized to
        completed; our retry observes IllegalTransition." The fix
        routes IllegalTransition to read-back; read-back finds an
        equivalent prior submission → success page.
        """
        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="itb"
        )
        original_submit = store.submit

        def fake_submit(task_id_, token, submission):
            # Commit our submission, accept it (state -> completed),
            # then raise IllegalTransition to simulate "response was
            # lost; we re-tried and saw a state the store now
            # rejects." Read-back must find our equivalent committed
            # submission and render success.
            original_submit(task_id_, token, submission)
            store.accept(task_id_)
            raise IllegalTransition("simulated post-terminalization retry")

        monkeypatch.setattr(store, "submit", fake_submit)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        # SUCCESS — this is the chunk-9c §K-2 bug, fixed.
        assert resp.status_code == 200
        assert child_sha in resp.text

    def test_completed_with_non_equivalent_prior_renders_conflict(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch,
    ) -> None:
        """IllegalTransition + read-back finds a different submission → conflict."""
        from eden_contracts import ImplementPayload, ImplementTask

        task_id, child_sha = _claim_and_prep(
            signed_in_impl_client, store, bare_repo, base_sha, slug="itc"
        )
        synthetic_task = ImplementTask(
            task_id=task_id,
            kind="implement",
            state="completed",
            payload=ImplementPayload(proposal_id="proposal-itc"),
            created_at="2026-04-24T11:00:00.000Z",
            updated_at="2026-04-24T13:00:00.000Z",
        )
        # A different worker's submission would have a different
        # trial_id; equivalence keys off (status, trial_id, commit_sha).
        non_equiv_prior = ImplementSubmission(
            status="success",
            trial_id="trial-other-worker",
            commit_sha=child_sha,
        )

        def fake_submit(*a, **k):
            raise IllegalTransition("task already terminal")

        # Patch read_task to return synthetic AFTER the route's
        # pre-submit reads run. Easiest: use a one-shot wrapper that
        # arms the patch on the first store.submit call.
        original_read_task = store.read_task
        original_read_submission = store.read_submission

        def arm_then_raise(*a, **k):
            monkeypatch.setattr(store, "read_task", lambda tid: synthetic_task)
            monkeypatch.setattr(
                store, "read_submission", lambda tid: non_equiv_prior
            )
            raise IllegalTransition("task already terminal")

        monkeypatch.setattr(store, "submit", arm_then_raise)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
            ],
        )
        # Restore so subsequent fixture cleanup works.
        monkeypatch.setattr(store, "read_task", original_read_task)
        monkeypatch.setattr(store, "read_submission", original_read_submission)
        assert resp.status_code == 502
        assert "conflicting-resubmission" in resp.text
        assert "operator intervention" in resp.text
