"""Issue #52 — parent_commits SHA hints on the ideator page.

The ideator landing AND draft pages must surface:

- the seed/base commit SHA from ``--base-commit-sha``
- the most recent integrated variants' ``variant_commit_sha``s
  (status=success AND variant_commit_sha is not None)

Both rendered as click-to-copy chips so the operator has something
to paste into the ``parent_commits`` field.
"""

from __future__ import annotations

from urllib.parse import urlencode

from conftest import BASE_SHA_FIXTURE, get_csrf, seed_evaluate_task
from eden_contracts import Variant
from eden_storage import EvaluationSubmission, InMemoryStore
from eden_web_ui.routes import ideator as ideator_routes
from fastapi.testclient import TestClient


def _post_form(client: TestClient, url: str, fields: list[tuple[str, str]]):
    body = urlencode(fields)
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def _seed_integrated_variant(
    store: InMemoryStore,
    *,
    variant_id: str,
    sha: str,
    slug: str = "v",
) -> None:
    """Drive a variant through the full lifecycle and integrate it.

    Mirrors what an end-to-end run produces: an executor-submitted
    variant that the evaluator scores ``success`` and the integrator
    stamps with ``variant_commit_sha`` per chapter 6 §3.4.
    """
    eval_task_id, _, _ = seed_evaluate_task(
        store, slug=slug, variant_id=variant_id
    )
    eval_claim = store.claim(eval_task_id, store._test_worker_ids["evaluator-w"])
    store.submit(
        eval_task_id,
        eval_claim.worker_id,
        EvaluationSubmission(
            status="success",
            variant_id=variant_id,
            evaluation={"score": 0.5},
        ),
    )
    store.accept(eval_task_id)
    store.integrate_variant(variant_id, sha)


class TestIdeatorListHints:
    def test_base_sha_renders_when_configured(
        self, signed_in_client_with_base_sha: TestClient
    ) -> None:
        resp = signed_in_client_with_base_sha.get("/ideator/")
        assert resp.status_code == 200
        assert "parent commit hints" in resp.text
        assert BASE_SHA_FIXTURE in resp.text
        assert 'class="copyable-sha"' in resp.text

    def test_base_sha_warning_when_not_configured(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/ideator/")
        assert resp.status_code == 200
        assert "not configured" in resp.text
        # The warning surfaces the missing CLI flag.
        assert "--base-commit-sha" in resp.text

    def test_recent_integrated_variants_render(
        self,
        signed_in_client_with_base_sha: TestClient,
        store: InMemoryStore,
    ) -> None:
        sha = "1" * 40
        _seed_integrated_variant(store, variant_id="variant-int-1", sha=sha)
        resp = signed_in_client_with_base_sha.get("/ideator/")
        assert resp.status_code == 200
        assert "recent integrated variants" in resp.text
        assert "variant-int-1" in resp.text
        assert sha in resp.text

    def test_starting_variants_excluded_from_hints(
        self,
        signed_in_client_with_base_sha: TestClient,
        store: InMemoryStore,
    ) -> None:
        # A variant in `starting` (no variant_commit_sha) must NOT appear
        # in the hints panel — chapter 6 §3.4 wires variant_commit_sha
        # only on integration. The variant may still appear in the
        # broader "recent variants" panel below.
        store.create_variant(
            Variant(
                variant_id="variant-pending",
                experiment_id=store.experiment_id,
                idea_id="idea-x",
                status="starting",
                parent_commits=["b" * 40],
                branch="work/x-variant-pending",
                started_at="2026-04-24T11:00:00Z",
            )
        )
        resp = signed_in_client_with_base_sha.get("/ideator/")
        # Slice to the hints panel.
        text = resp.text
        start = text.index('class="parent-commits-hints"')
        end = text.index("</section>", start)
        hints_block = text[start:end]
        assert "variant-pending" not in hints_block


class TestIdeatorDraftHints:
    def test_draft_form_includes_hints_after_claim(
        self,
        signed_in_client_with_base_sha: TestClient,
        store: InMemoryStore,
    ) -> None:
        sha = "9" * 40
        _seed_integrated_variant(store, variant_id="variant-int-7", sha=sha)
        store.create_ideation_task("t-hint")
        csrf = get_csrf(signed_in_client_with_base_sha)
        _post_form(
            signed_in_client_with_base_sha,
            "/ideator/t-hint/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_client_with_base_sha.get("/ideator/t-hint/draft")
        assert resp.status_code == 200
        assert BASE_SHA_FIXTURE in resp.text
        assert sha in resp.text
        assert "variant-int-7" in resp.text


class TestHelpers:
    def test_list_recent_integrated_variants_filters(
        self, store: InMemoryStore
    ) -> None:
        # One integrated, one starting, one error.
        _seed_integrated_variant(
            store, variant_id="variant-ok", sha="d" * 40, slug="ok"
        )
        store.create_variant(
            Variant(
                variant_id="variant-pending",
                experiment_id=store.experiment_id,
                idea_id="idea-pending",
                status="starting",
                parent_commits=["b" * 40],
                branch="work/pending-variant-pending",
                started_at="2026-04-24T11:00:00Z",
            )
        )
        out = ideator_routes._list_recent_integrated_variants(store)
        assert [v.variant_id for v in out] == ["variant-ok"]
