"""Tests for issues #134 (Pydantic ValidationError → form re-render) and
#158 (broaden web-ui exception coverage to all typed StorageError subclasses).

The bug shape for both: a form route raised an uncaught exception
(``pydantic.ValidationError`` for #134; an untyped ``StorageError``
subclass like ``NotFound`` / ``WorkerNotRegistered`` for #158) and
FastAPI surfaced it as a generic HTTP 500. The fix wraps each
construction / store call in a typed catch that routes the operator
to a re-rendered draft form (for Pydantic) or a wire-error banner
(for store rejections).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import pytest
from conftest import (
    get_csrf,
    make_child_commit,
    seed_implement_task,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore
from eden_web_ui.routes import (
    evaluator as evaluator_routes,
)
from eden_web_ui.routes import (
    executor as executor_routes,
)
from eden_web_ui.routes import (
    ideator as ideator_routes,
)
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
    ideator_routes._CLAIMS.clear()
    ideator_routes._DRAFT_BUFFERS.clear()
    executor_routes._CLAIMS.clear()
    evaluator_routes._CLAIMS.clear()
    yield
    ideator_routes._CLAIMS.clear()
    ideator_routes._DRAFT_BUFFERS.clear()
    executor_routes._CLAIMS.clear()
    evaluator_routes._CLAIMS.clear()


class TestPydanticValidationReRenders:
    """Issue #134: ValidationError raised by Idea / Variant /
    *Submission construction → form re-render, not HTTP 500."""

    def test_ideator_uppercase_slug_re_renders_with_field_error(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """The demo trigger: operator types ``Spanish`` (capital S).

        The form validator accepts ``Spanish`` because its grammar
        allows alphanumerics; ``Idea.slug`` is strict-lowercase
        (``^[a-z0-9][a-z0-9-]*$``). Pre-fix this raised
        ``pydantic.ValidationError`` and surfaced as HTTP 500. Post-fix:
        the route re-renders the draft form (HTTP 400) with the
        field-level error and preserved input, no Idea created.
        """
        store.create_ideation_task("t-uppercase-slug")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-uppercase-slug/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-uppercase-slug/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "Spanish",  # capital S — fails Idea.slug pattern
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "## why\n\nbecause",
            },
        )
        # Not a 500.
        assert resp.status_code == 400, resp.text
        # User input preserved so the operator can fix and resubmit.
        assert "Spanish" in resp.text
        # Field error surfaces (pydantic's pattern-mismatch message).
        assert "slug" in resp.text.lower()
        # No store mutation; claim still owned by the operator.
        assert store.list_ideas() == []
        assert store.read_task("t-uppercase-slug").state == "claimed"

    def test_ideator_slug_validation_failure_leaves_no_orphan_artifact(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        """Regression: pre-fix, ``_persist_idea_drafts`` wrote the
        artifact to ``artifacts_dir/<idea_id>.md`` BEFORE constructing
        the ``Idea``. A ValidationError on construction left the
        artifact file on disk with no Idea pointing at it. Post-fix,
        the artifact URI is computed (not written) before validation,
        the Idea is constructed (validation barrier), and only then is
        the artifact written. On a slug rejection the artifacts_dir
        should remain empty.
        """
        store.create_ideation_task("t-no-leak")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-no-leak/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        # Sanity: no leftover state in artifacts_dir from any prior
        # phase. (The fixture starts empty.)
        assert list(artifacts_dir.iterdir()) == []
        resp = signed_in_client.post(
            "/ideator/t-no-leak/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "Spanish",  # rejected by Idea.slug pattern
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "## why\n\nbecause",
            },
        )
        assert resp.status_code == 400
        assert store.list_ideas() == []
        # Critically: no artifact file leaked.
        assert list(artifacts_dir.iterdir()) == []

    def test_ideator_underscore_slug_re_renders_with_field_error(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """Form grammar accepts underscore; ``Idea.slug`` rejects it."""
        store.create_ideation_task("t-underscore-slug")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-underscore-slug/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-underscore-slug/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "feat_one",  # underscore — fails Idea.slug pattern
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "## why\n\nbecause",
            },
        )
        assert resp.status_code == 400, resp.text
        assert "feat_one" in resp.text
        assert store.list_ideas() == []


class TestClaimStorageErrorCoverage:
    """Issue #158: claim handlers must catch the full reachable set of
    StorageError subclasses (not just IllegalTransition /
    InvalidPrecondition) and route to a banner redirect, not 500."""

    def test_ideator_claim_missing_task_routes_to_banner(
        self, signed_in_client: TestClient
    ) -> None:
        """claim on an unknown task_id raises ``NotFound``. Pre-fix this
        leaked as HTTP 500. Post-fix: redirect with ``eden://error/not-found``
        banner on the list page."""
        token = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/ideator/no-such-task/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/ideator/?banner=")
        assert "not-found" in location

    def test_executor_claim_missing_task_routes_to_banner(
        self,
        signed_in_impl_client: TestClient,
        bare_repo: GitRepo,
    ) -> None:
        token = get_csrf(signed_in_impl_client)
        resp = signed_in_impl_client.post(
            "/executor/no-such-task/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/executor/?banner=")
        assert "not-found" in location

    def test_evaluator_claim_missing_task_routes_to_banner(
        self, signed_in_client: TestClient
    ) -> None:
        """The evaluator claim handler also reads the task first to pin
        the variant_id from its payload; that read raises ``NotFound``
        for an unknown task_id. Either path must produce a banner, not
        a 500."""
        token = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/evaluator/no-such-task/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/evaluator/?banner=")
        assert "not-found" in location


class TestExecutorSlugTriggeredByIntegrityCheck:
    """Issue #134 (defensive): the form layer already pre-validates
    commit_sha hex, so direct executor ValidationError triggers are
    rare. This test verifies the existing happy-path still routes
    correctly after the new ValidationError wrap around the Variant /
    VariantSubmission constructions."""

    def test_executor_happy_path_unaffected(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(
            store, base_sha=base_sha, slug="post-fix-smoke"
        )
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        child_sha = make_child_commit(bare_repo, base_sha, "post-fix")
        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
                ("description", "smoke"),
            ],
        )
        assert resp.status_code == 200, resp.text
        assert store.read_task(task_id).state == "submitted"
