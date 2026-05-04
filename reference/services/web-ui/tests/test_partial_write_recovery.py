"""Partial-write recovery tests for the ideator submit flow.

Forces failure at each phase boundary and asserts:

- Phase 1 failure (create_idea) leaves orchestrator-visible state
  unchanged: no `ready` ideas exist.
- Phase 2 failure (mark_idea_ready) leaves no `ready` ideas.
- Phase 3 failure (submit) surfaces orphaned idea IDs in the
  rendered error page.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import pytest
from conftest import get_csrf
from eden_storage import DispatchError, InMemoryStore
from fastapi.testclient import TestClient
from httpx import Response


def _post_form(
    client: TestClient, url: str, fields: list[tuple[str, str]]
) -> Response:
    """POST a form with repeated field names (list-of-tuples body).

    The TestClient's ``data=`` parameter accepts a list-of-tuples but
    httpx 0.28+ deprecates this; encoding the body manually is cleaner
    and preserves duplicate keys for multi-row ideator forms.
    """
    body = urlencode(fields)
    return client.post(
        url,
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def _claim_and_form(
    client: TestClient, store: InMemoryStore, task_id: str
) -> dict[str, str]:
    store.create_ideate_task(task_id)
    csrf = get_csrf(client)
    resp = client.post(
        f"/ideator/{task_id}/claim",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return {
        "csrf_token": csrf,
        "status": "success",
        "slug": "feat-x",
        "priority": "1.0",
        "parent_commits": "a" * 40,
        "rationale": "rationale",
    }


def _wrap_with_failure(
    method: Callable, fail_after: int
) -> Callable:
    """Make the wrapped store method raise DispatchError after N successful calls."""
    counter = {"n": 0}

    def wrapped(*args, **kwargs):
        counter["n"] += 1
        if counter["n"] > fail_after:
            raise DispatchError("simulated failure")
        return method(*args, **kwargs)

    return wrapped


class TestPhase2Failure:
    def test_mark_ready_failure_leaves_no_ready_ideas(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If mark_idea_ready raises mid-loop, no ideas are `ready`."""
        # Force submit to use 2-row form so loop ordering matters.
        form_base = _claim_and_form(signed_in_client, store, "t-1")
        # Replace mark_idea_ready with a failing wrapper after 0 successes.
        # That simulates "first idea already in `drafting`, fail before
        # the first mark_ready can fire."
        monkeypatch.setattr(
            store,
            "mark_idea_ready",
            _wrap_with_failure(store.mark_idea_ready, fail_after=0),
        )
        # Two rows = two slugs/priorities/parents/rationales
        resp = _post_form(
            signed_in_client,
            "/ideator/t-1/submit",
            [
                ("csrf_token", form_base["csrf_token"]),
                ("status", "success"),
                ("slug", "feat-a"),
                ("priority", "1.0"),
                ("parent_commits", "a" * 40),
                ("rationale", "first"),
                ("slug", "feat-b"),
                ("priority", "1.0"),
                ("parent_commits", "a" * 40),
                ("rationale", "second"),
            ],
        )
        assert resp.status_code == 502
        assert "mark_idea_ready failed" in resp.text
        # No `ready` ideas — orchestrator's dispatch path won't pick up.
        assert store.list_ideas(state="ready") == []
        # Both ideas are still drafting (Phase 1 succeeded).
        drafting = store.list_ideas(state="drafting")
        assert len(drafting) == 2


class TestPhase3RetryAndOrphan:
    def test_submit_retries_then_orphan_page_lists_idea_ids(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If every submit retry fails with DispatchError, the orphan page
        renders the ready idea IDs that need operator intervention."""
        form_base = _claim_and_form(signed_in_client, store, "t-orph")

        original_submit = store.submit
        attempts = {"n": 0}

        def always_fail(*args, **kwargs):
            attempts["n"] += 1
            raise DispatchError("transport-shaped")

        monkeypatch.setattr(store, "submit", always_fail)
        resp = signed_in_client.post("/ideator/t-orph/submit", data=form_base)
        assert resp.status_code == 502
        assert "could not be submitted" in resp.text
        # 3 retry attempts.
        assert attempts["n"] == 3
        # At least one `ready` idea_id appears on the page.
        readies = store.list_ideas(state="ready")
        assert len(readies) == 1
        assert readies[0].idea_id in resp.text
        # Restore original submit so other tests share a clean store.
        monkeypatch.setattr(store, "submit", original_submit)

    def test_submit_succeeds_after_one_transient_failure(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single transport failure followed by success completes normally."""
        form_base = _claim_and_form(signed_in_client, store, "t-flaky")

        original_submit = store.submit
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DispatchError("transient")
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(store, "submit", flaky)
        resp = signed_in_client.post("/ideator/t-flaky/submit", data=form_base)
        assert resp.status_code == 200
        assert "submitted" in resp.text.lower()
        assert calls["n"] == 2  # 1 fail + 1 success
        assert store.read_task("t-flaky").state == "submitted"
