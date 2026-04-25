"""Per-route unit tests for the admin module (chunk 9e)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    WORKER_ID,
    _config,
    _now,
    get_csrf,
    make_child_commit,
    seed_evaluate_task,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _seed_plan_task(store: InMemoryStore, task_id: str = "plan-A") -> str:
    store.create_plan_task(task_id)
    return task_id


def _claim_plan_task(
    store: InMemoryStore, task_id: str, *, worker_id: str = "ui-w"
) -> str:
    claim = store.claim(task_id, worker_id)
    return claim.token


class TestAdminAuthGate:
    def test_get_index_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_reclaim_unauthenticated_redirects_signin(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_plan_task(store)
        _claim_plan_task(store, task_id)
        resp = client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": "anything"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_work_refs_delete_unauthenticated_redirects_signin(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/work-refs/delete",
            data={"csrf_token": "x", "ref_name": "refs/heads/work/foo"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"


class TestAdminIndex:
    def test_renders_with_seeded_state(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        _seed_plan_task(store, "plan-B")
        store.create_plan_task("plan-C")
        store.claim("plan-C", "w-1")
        resp = signed_in_client.get("/admin/")
        assert resp.status_code == 200
        assert "admin dashboard" in resp.text
        # 2 pending plan tasks + 1 claimed plan task seeded above.
        assert "tasks by kind" in resp.text


class TestAdminTasks:
    def test_no_filter_lists_all(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        store.create_plan_task("plan-B")
        resp = signed_in_client.get("/admin/tasks/")
        assert resp.status_code == 200
        assert "plan-A" in resp.text
        assert "plan-B" in resp.text

    def test_kind_filter(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        resp = signed_in_client.get("/admin/tasks/?kind=evaluate")
        assert resp.status_code == 200
        assert "plan-A" not in resp.text

    def test_state_filter(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        store.create_plan_task("plan-B")
        store.claim("plan-B", "w-1")
        resp = signed_in_client.get("/admin/tasks/?state=claimed")
        assert resp.status_code == 200
        assert "plan-B" in resp.text
        assert "plan-A" not in resp.text

    def test_unknown_kind_renders_empty(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """Plan §A.3: unknown filter values must yield empty rowset, not full list."""
        _seed_plan_task(store, "plan-A")
        resp = signed_in_client.get("/admin/tasks/?kind=garbage")
        assert resp.status_code == 200
        assert "plan-A" not in resp.text
        assert "no tasks match" in resp.text

    def test_unknown_state_renders_empty(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        resp = signed_in_client.get("/admin/tasks/?state=lol")
        assert resp.status_code == 200
        assert "plan-A" not in resp.text
        assert "no tasks match" in resp.text


class TestAdminTaskDetail:
    def test_pending_task_shows_no_reclaim_button(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        resp = signed_in_client.get("/admin/tasks/plan-A/")
        assert resp.status_code == 200
        assert "reclaim" not in resp.text.lower()

    def test_claimed_task_shows_reclaim_button(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        store.claim("plan-A", "w-1")
        resp = signed_in_client.get("/admin/tasks/plan-A/")
        assert resp.status_code == 200
        assert ">reclaim<" in resp.text

    def test_submitted_task_shows_force_reclaim_button(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        from eden_storage import PlanSubmission

        task_id = _seed_plan_task(store, "plan-A")
        token = _claim_plan_task(store, task_id)
        store.submit(task_id, token, PlanSubmission(status="success", proposal_ids=()))
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/")
        assert resp.status_code == 200
        assert "force-reclaim" in resp.text

    def test_terminal_task_shows_no_reclaim_button(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        from eden_storage import PlanSubmission

        task_id = _seed_plan_task(store, "plan-A")
        token = _claim_plan_task(store, task_id)
        store.submit(task_id, token, PlanSubmission(status="success", proposal_ids=()))
        store.accept(task_id)
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/")
        assert resp.status_code == 200
        # "force-reclaim" or plain "reclaim" — neither should appear as a button now
        assert "<button" not in resp.text or "reclaim" not in resp.text

    def test_nonexistent_task_returns_404(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/tasks/does-not-exist/")
        assert resp.status_code == 404


class TestAdminTaskReclaim:
    def test_happy_path_claimed_to_pending(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_plan_task(store, "plan-A")
        _claim_plan_task(store, task_id)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reclaimed=ok" in resp.headers["location"]
        task = store.read_task(task_id)
        assert task.state == "pending"

    def test_submitted_to_pending(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        from eden_storage import PlanSubmission

        task_id = _seed_plan_task(store, "plan-A")
        token = _claim_plan_task(store, task_id)
        store.submit(task_id, token, PlanSubmission(status="success", proposal_ids=()))
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reclaimed=ok" in resp.headers["location"]
        assert store.read_task(task_id).state == "pending"

    def test_terminal_returns_illegal_transition(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        from eden_storage import PlanSubmission

        task_id = _seed_plan_task(store, "plan-A")
        token = _claim_plan_task(store, task_id)
        store.submit(task_id, token, PlanSubmission(status="success", proposal_ids=()))
        store.accept(task_id)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=illegal-transition" in resp.headers["location"]

    def test_csrf_mismatch_returns_403(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_plan_task(store, "plan-A")
        _claim_plan_task(store, task_id)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": "wrong-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestAdminTrials:
    def test_lists_trials_with_status_filter(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="t1", trial_id="trial-X", artifacts_dir=artifacts_dir
        )
        resp = signed_in_client.get("/admin/trials/")
        assert resp.status_code == 200
        assert "trial-X" in resp.text
        resp_filtered = signed_in_client.get("/admin/trials/?status=success")
        assert resp_filtered.status_code == 200
        # Trial is in "starting" state, so success filter excludes it
        assert "trial-X" not in resp_filtered.text

    def test_trial_detail_renders(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="t1", trial_id="trial-Y", artifacts_dir=artifacts_dir
        )
        resp = signed_in_client.get("/admin/trials/trial-Y/")
        assert resp.status_code == 200
        assert "trial-Y" in resp.text
        assert "proposal-t1" in resp.text


class TestAdminEvents:
    def test_renders_events(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        for i in range(5):
            _seed_plan_task(store, f"plan-{i}")
        resp = signed_in_client.get("/admin/events/")
        assert resp.status_code == 200
        assert "task.created" in resp.text

    def test_limit_clamped(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        # Limit > 1000 must be clamped to 1000.
        resp = signed_in_client.get("/admin/events/?limit=99999")
        assert resp.status_code == 200

    def test_type_filter(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        store.claim("plan-A", "w-1")
        resp = signed_in_client.get("/admin/events/?type=task.claimed")
        assert resp.status_code == 200
        assert "task.claimed" in resp.text


@pytest.fixture
def admin_repo_app(
    store: InMemoryStore, artifacts_dir: Path, bare_repo: GitRepo
) -> FastAPI:
    return make_app(
        store=store,
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        secure_cookies=False,
        now=_now,
        repo=bare_repo,
    )


@pytest.fixture
def signed_in_admin_repo_client(admin_repo_app: FastAPI):
    with TestClient(admin_repo_app) as c:
        resp = c.post("/signin", follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestAdminWorkRefs:
    def test_no_repo_renders_placeholder(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/work-refs/")
        assert resp.status_code == 200
        assert "requires <code>--repo-path</code>" in resp.text

    def test_no_refs_renders_empty_groups(
        self, signed_in_admin_repo_client: TestClient
    ) -> None:
        resp = signed_in_admin_repo_client.get("/admin/work-refs/")
        assert resp.status_code == 200
        assert "eligible for deletion (0)" in resp.text

    def test_eligible_ref_listed(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        # Seed an evaluate flow that produces a starting+success trial,
        # then mark it eval_error so it's terminal-handled.
        seed_evaluate_task(
            store, slug="t1", trial_id="trial-E", artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "abc"),
        )
        # Create the work ref pointing at the trial's commit_sha.
        trial = store.read_trial("trial-E")
        assert trial.branch is not None
        assert trial.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{trial.branch}", trial.commit_sha)
        # Mark trial terminal so it's GC-eligible.
        store.declare_trial_eval_error("trial-E")
        resp = signed_in_admin_repo_client.get("/admin/work-refs/")
        assert resp.status_code == 200
        assert trial.branch in resp.text
        assert "eligible for deletion (1)" in resp.text

    def test_starting_trial_listed_as_not_eligible(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="t2", trial_id="trial-N", artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "abc"),
        )
        trial = store.read_trial("trial-N")
        assert trial.branch is not None
        assert trial.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{trial.branch}", trial.commit_sha)
        # trial is "starting"
        resp = signed_in_admin_repo_client.get("/admin/work-refs/")
        assert resp.status_code == 200
        assert "not eligible (1)" in resp.text

    def test_orphan_ref_listed(
        self,
        signed_in_admin_repo_client: TestClient,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        sha = make_child_commit(bare_repo, base_sha, "orphan")
        bare_repo.create_ref("refs/heads/work/lone-trial", sha)
        resp = signed_in_admin_repo_client.get("/admin/work-refs/")
        assert resp.status_code == 200
        assert "orphan refs (1)" in resp.text
        assert "work/lone-trial" in resp.text


class TestAdminWorkRefsDelete:
    def test_invalid_ref_name_rejected(
        self, signed_in_admin_repo_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={"csrf_token": csrf, "ref_name": "refs/heads/trial/foo"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=invalid-ref-name" in resp.headers["location"]

    def test_path_traversal_rejected(
        self, signed_in_admin_repo_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={
                "csrf_token": csrf,
                "ref_name": "refs/heads/work/../trial/x",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=invalid-ref-name" in resp.headers["location"]

    def test_csrf_mismatch_returns_403(
        self, signed_in_admin_repo_client: TestClient
    ) -> None:
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={"csrf_token": "wrong", "ref_name": "refs/heads/work/foo"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_not_eligible_ref_refuses(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="t3", trial_id="trial-NE", artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "ne"),
        )
        trial = store.read_trial("trial-NE")
        assert trial.branch is not None
        assert trial.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{trial.branch}", trial.commit_sha)
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={
                "csrf_token": csrf,
                "ref_name": f"refs/heads/{trial.branch}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=not-eligible" in resp.headers["location"]
        # ref still exists
        assert bare_repo.resolve_ref(f"refs/heads/{trial.branch}") is not None

    def test_happy_path_eligible_deletion(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="t4", trial_id="trial-G", artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "g"),
        )
        trial = store.read_trial("trial-G")
        assert trial.branch is not None
        assert trial.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{trial.branch}", trial.commit_sha)
        store.declare_trial_eval_error("trial-G")
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={
                "csrf_token": csrf,
                "ref_name": f"refs/heads/{trial.branch}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?deleted=ok" in resp.headers["location"]
        assert bare_repo.resolve_ref(f"refs/heads/{trial.branch}") is None

    def test_orphan_ref_deletion(
        self,
        signed_in_admin_repo_client: TestClient,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        sha = make_child_commit(bare_repo, base_sha, "orph")
        bare_repo.create_ref("refs/heads/work/lone", sha)
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={"csrf_token": csrf, "ref_name": "refs/heads/work/lone"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?deleted=ok" in resp.headers["location"]
        assert bare_repo.resolve_ref("refs/heads/work/lone") is None
