"""Admin lifecycle UI tests (12a-3 wave 5).

Covers:

- ``/admin/ideas/`` list page (state filter; invalid filter → empty
  rowset per chunk-9e discipline).
- ``/admin/ideas/{idea_id}/`` detail page (idea record, live execution
  tasks, create-execution-task form visibility based on state).
- ``POST /admin/ideas/{idea_id}/create-execution-task`` (target=none /
  worker / group; idea.intended_executor inheritance; closed-allowlist
  banner outcomes).
- ``/admin/experiment/`` detail page (running / terminated state;
  terminate form visibility; idempotent re-terminate).
- ``POST /admin/experiment/terminate`` (reason required, missing-reason
  banner; admin-disabled banner; idempotent re-terminate; CSRF;
  unauthenticated redirect).
- Dashboard banner when experiment is terminated.
"""

from __future__ import annotations

from conftest import get_csrf
from eden_contracts import ExecutionTask, Idea, TaskTarget
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _seed_idea(
    store: InMemoryStore,
    *,
    idea_id: str = "idea-1",
    slug: str = "x",
    ready: bool = True,
    intended_executor: TaskTarget | None = None,
) -> Idea:
    kwargs = {
        "idea_id": idea_id,
        "experiment_id": store.experiment_id,
        "slug": slug,
        "priority": 1.0,
        "parent_commits": ["a" * 40],
        "artifacts_uri": f"file:///tmp/{idea_id}.md",
        "state": "drafting",
        "created_at": "2026-05-01T00:00:00Z",
    }
    if intended_executor is not None:
        kwargs["intended_executor"] = intended_executor
    idea = Idea(**kwargs)
    store.create_idea(idea)
    if ready:
        store.mark_idea_ready(idea_id)
    return store.read_idea(idea_id)


class TestAdminIdeasList:
    def test_unauthenticated_redirects(self, client: TestClient) -> None:
        resp = client.get("/admin/ideas/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_empty(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.get("/admin/ideas/")
        assert resp.status_code == 200
        assert "no ideas match" in resp.text

    def test_renders_with_intended_executor(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(
            store,
            idea_id="idea-1",
            intended_executor=TaskTarget(kind="worker", id="executor-w"),
        )
        _seed_idea(store, idea_id="idea-2")
        resp = signed_in_client.get("/admin/ideas/")
        assert resp.status_code == 200
        assert "idea-1" in resp.text
        assert "idea-2" in resp.text
        # intended_executor renders as kind:id.
        assert "worker:executor-w" in resp.text

    def test_state_filter(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-ready", ready=True)
        _seed_idea(store, idea_id="idea-draft", ready=False)
        resp = signed_in_client.get("/admin/ideas/?state=ready")
        assert resp.status_code == 200
        assert "idea-ready" in resp.text
        assert "idea-draft" not in resp.text

    def test_invalid_state_filter_empty_rowset(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store)
        resp = signed_in_client.get("/admin/ideas/?state=bogus")
        assert resp.status_code == 200
        assert "no ideas match" in resp.text


class TestAdminIdeaDetail:
    def test_unauthenticated_redirects(self, client: TestClient) -> None:
        resp = client.get("/admin/ideas/idea-x/", follow_redirects=False)
        assert resp.status_code == 303

    def test_renders_idea_record(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(
            store,
            idea_id="idea-1",
            intended_executor=TaskTarget(kind="group", id="humans"),
        )
        resp = signed_in_client.get("/admin/ideas/idea-1/")
        assert resp.status_code == 200
        assert "idea-1" in resp.text
        assert "group:humans" in resp.text
        # Create-execution-task form is visible for `ready` idea.
        assert "create execution task" in resp.text

    def test_create_form_hidden_for_drafting(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-d", ready=False)
        resp = signed_in_client.get("/admin/ideas/idea-d/")
        assert resp.status_code == 200
        # The drafting idea cannot have an execution task created against
        # it; the form is replaced with an explanatory note.
        assert 'name="target_kind"' not in resp.text

    def test_create_form_hidden_when_live_execution_exists(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-1")
        store.create_execution_task("exec-1", "idea-1")
        resp = signed_in_client.get("/admin/ideas/idea-1/")
        assert resp.status_code == 200
        # Live execution task is shown but the create form is hidden.
        assert "exec-1" in resp.text
        assert 'name="target_kind"' not in resp.text

    def test_unknown_idea_404(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/ideas/missing/")
        assert resp.status_code == 404


class TestCreateExecutionTaskPost:
    def test_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        """Auth-first POST discipline: 303 to /signin before CSRF check."""
        resp = client.post(
            "/admin/ideas/idea-x/create-execution-task",
            data={"csrf_token": "anything", "target_kind": "none"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_csrf_mismatch_403(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-1")
        resp = signed_in_client.post(
            "/admin/ideas/idea-1/create-execution-task",
            data={"csrf_token": "wrong", "target_kind": "none"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_target_none_inherits_intended_executor(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(
            store,
            idea_id="idea-1",
            intended_executor=TaskTarget(kind="worker", id="executor-w"),
        )
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/idea-1/create-execution-task",
            data={"csrf_token": csrf, "target_kind": "none", "target_id": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/ideas/idea-1/?created=ok"
        # The new task carries the idea's intended_executor as target.
        tasks = store.list_tasks(kind="execution")
        assert len(tasks) == 1
        task = tasks[0]
        assert isinstance(task, ExecutionTask)
        assert task.payload.idea_id == "idea-1"
        assert task.target is not None
        assert task.target.kind == "worker"
        assert task.target.id == "executor-w"

    def test_explicit_target_overrides_intended_executor(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(
            store,
            idea_id="idea-1",
            intended_executor=TaskTarget(kind="worker", id="executor-w"),
        )
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/idea-1/create-execution-task",
            data={
                "csrf_token": csrf,
                "target_kind": "group",
                "target_id": "humans",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        tasks = store.list_tasks(kind="execution")
        assert len(tasks) == 1
        task = tasks[0]
        assert isinstance(task, ExecutionTask)
        assert task.target is not None
        assert task.target.kind == "group"
        assert task.target.id == "humans"

    def test_invalid_target_grammar_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-1")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/idea-1/create-execution-task",
            data={
                "csrf_token": csrf,
                "target_kind": "worker",
                "target_id": "BAD-CAPS",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?error=invalid-target")
        # No task created.
        assert store.list_tasks(kind="execution") == []

    def test_unknown_kind_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-1")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/idea-1/create-execution-task",
            data={"csrf_token": csrf, "target_kind": "team", "target_id": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?error=invalid-target")

    def test_idea_not_ready_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-d", ready=False)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/idea-d/create-execution-task",
            data={"csrf_token": csrf, "target_kind": "none"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == "/admin/ideas/idea-d/?error=invalid-precondition"
        )

    def test_idea_not_found_banner(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/missing/create-execution-task",
            data={"csrf_token": csrf, "target_kind": "none"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?error=not-found")

    def test_terminated_experiment_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_idea(store, idea_id="idea-1")
        # Need a worker_id matching the §6.1 grammar; the test fixture
        # auto-registers "ui-w" as the session worker_id.
        store.terminate_experiment(reason="x", terminated_by="ui-w")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/ideas/idea-1/create-execution-task",
            data={"csrf_token": csrf, "target_kind": "none"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?error=illegal-transition")


class TestAdminExperimentDetail:
    def test_unauthenticated_redirects(self, client: TestClient) -> None:
        resp = client.get("/admin/experiment/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_renders_running_state(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/experiment/")
        assert resp.status_code == 200
        assert "running" in resp.text
        # Terminate form visible while running.
        assert 'name="reason"' in resp.text

    def test_renders_terminated_state(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.terminate_experiment(reason="done for test", terminated_by="ui-w")
        resp = signed_in_client.get("/admin/experiment/")
        assert resp.status_code == 200
        assert "terminated" in resp.text
        # Termination record renders the reason + terminated_by.
        assert "done for test" in resp.text
        assert "ui-w" in resp.text
        # Terminate form is hidden when already terminated.
        assert 'name="reason"' not in resp.text


class TestTerminateExperimentPost:
    def test_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/experiment/terminate",
            data={"csrf_token": "anything", "reason": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_csrf_mismatch_403(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.post(
            "/admin/experiment/terminate",
            data={"csrf_token": "wrong", "reason": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_missing_reason_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/experiment/terminate",
            data={"csrf_token": csrf, "reason": "   "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == "/admin/experiment/?error=missing-reason"
        )
        # Experiment still running.
        assert store.read_experiment_state() == "running"

    def test_terminate_succeeds(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/experiment/terminate",
            data={"csrf_token": csrf, "reason": "max variants reached"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"] == "/admin/experiment/?terminated=ok"
        )
        assert store.read_experiment_state() == "terminated"
        # Event recorded with the operator's reason + worker_id.
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        assert term_events[0].data["reason"] == "max variants reached"
        assert term_events[0].data["terminated_by"] == "ui-w"

    def test_idempotent_repeat_already_terminated(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # Pre-seed a termination from a different reason.
        store.terminate_experiment(
            reason="initial reason", terminated_by="other-w"
        )
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/experiment/terminate",
            data={"csrf_token": csrf, "reason": "second attempt"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Idempotency: state still terminated; banner reflects the
        # already-terminated path so the operator isn't surprised.
        assert (
            resp.headers["location"]
            == "/admin/experiment/?terminated=already-terminated"
        )
        # The original reason is preserved.
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        assert term_events[0].data["reason"] == "initial reason"

    def test_admin_disabled_banner(
        self, client_no_admin: TestClient
    ) -> None:
        # Sign in to the admin-disabled fixture.
        signin = client_no_admin.post("/signin", follow_redirects=False)
        assert signin.status_code == 303
        csrf = get_csrf(client_no_admin)
        resp = client_no_admin.post(
            "/admin/experiment/terminate",
            data={"csrf_token": csrf, "reason": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == "/admin/experiment/?error=admin-disabled"
        )


class TestAdminDashboardLifecycleBanner:
    def test_running_shows_no_banner(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/")
        assert resp.status_code == 200
        assert "experiment is" not in resp.text

    def test_terminated_shows_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.terminate_experiment(reason="done", terminated_by="ui-w")
        resp = signed_in_client.get("/admin/")
        assert resp.status_code == 200
        assert "experiment is" in resp.text
        assert "terminated" in resp.text


class TestIdeatorIntendedExecutorFlow:
    def test_submit_stamps_intended_executor(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """Full ideator path: claim → submit with intended_executor → idea persists with it."""
        # Pre-register an executor worker the operator can name.
        store.register_worker("custom-executor")
        store.create_ideation_task("plan-1")
        csrf = get_csrf(signed_in_client)
        # Claim via ideator route.
        claim = signed_in_client.post(
            "/ideator/plan-1/claim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert claim.status_code == 303

        resp = signed_in_client.post(
            "/ideator/plan-1/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "slug": "feat-a",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "do the thing",
                "intended_executor_kind": "worker",
                "intended_executor_id": "custom-executor",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200, resp.text
        ideas = store.list_ideas()
        assert len(ideas) == 1
        assert ideas[0].intended_executor is not None
        assert ideas[0].intended_executor.kind == "worker"
        assert ideas[0].intended_executor.id == "custom-executor"

    def test_invalid_intended_executor_id_renders_error(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("plan-1")
        csrf = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/plan-1/claim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/plan-1/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "slug": "x",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "y",
                "intended_executor_kind": "worker",
                "intended_executor_id": "BAD-CAPS",
            },
            follow_redirects=False,
        )
        # Validation error → form re-renders at 400.
        assert resp.status_code == 400
        # No idea was created on failure.
        assert store.list_ideas() == []

    def test_intended_executor_none_default(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """Operator omits intended_executor → idea has no routing hint."""
        store.create_ideation_task("plan-1")
        csrf = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/plan-1/claim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        signed_in_client.post(
            "/ideator/plan-1/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "slug": "x",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "y",
                "intended_executor_kind": "none",
                "intended_executor_id": "",
            },
            follow_redirects=False,
        )
        ideas = store.list_ideas()
        assert len(ideas) == 1
        assert ideas[0].intended_executor is None
