"""Multi-file evaluator submission flow (issue #120)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

import pytest
from conftest import (
    get_csrf,
    get_evaluate_submission,
    seed_evaluate_task,
)
from eden_storage import InMemoryStore
from eden_web_ui.routes import evaluator as evaluator_routes
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_claims():
    evaluator_routes._CLAIMS.clear()
    yield
    evaluator_routes._CLAIMS.clear()


def _claim(client: TestClient, eval_id: str, csrf: str) -> None:
    resp = client.post(
        f"/evaluator/{eval_id}/claim",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303


class TestArtifactBundle:
    def test_text_plus_files_writes_bundle(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        eval_id, variant_id, _ = seed_evaluate_task(
            store, artifacts_dir=artifacts_dir
        )
        csrf = get_csrf(signed_in_client)
        _claim(signed_in_client, eval_id, csrf)

        resp = signed_in_client.post(
            f"/evaluator/{eval_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "metric.score": "0.5",
                "artifact_text": "## eval notes\n\nthis variant ran cleanly",
                "artifacts_uri": "",
            },
            files={"artifact_files": ("perf.svg", b"<svg/>", "image/svg+xml")},
        )
        assert resp.status_code == 200, resp.text
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.artifacts_uri is not None
        assert recorded.artifacts_uri.endswith(".tar.gz")

        # The serving route streams entries by name out of the bundle.
        uri = recorded.artifacts_uri
        m = signed_in_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=evaluation.md"
        )
        assert m.status_code == 200
        assert "eval notes" in m.text

        s = signed_in_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=perf.svg"
        )
        assert s.status_code == 200
        assert s.content == b"<svg/>"

    def test_only_text_writes_single_md(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(
            store, artifacts_dir=artifacts_dir
        )
        csrf = get_csrf(signed_in_client)
        _claim(signed_in_client, eval_id, csrf)

        resp = signed_in_client.post(
            f"/evaluator/{eval_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "metric.score": "0.5",
                "artifact_text": "## eval notes",
                "artifacts_uri": "",
            },
        )
        assert resp.status_code == 200, resp.text
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.artifacts_uri is not None
        parsed = urlparse(recorded.artifacts_uri)
        assert parsed.path.endswith(".md")
        assert "eval notes" in Path(parsed.path).read_text()

    def test_explicit_uri_overrides_bundling(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(
            store, artifacts_dir=artifacts_dir
        )
        csrf = get_csrf(signed_in_client)
        _claim(signed_in_client, eval_id, csrf)

        explicit = "https://example.invalid/eval-output.html"
        resp = signed_in_client.post(
            f"/evaluator/{eval_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "metric.score": "0.5",
                # Both an explicit URI AND text+files — explicit wins.
                "artifact_text": "ignored",
                "artifacts_uri": explicit,
            },
            files={"artifact_files": ("ignored.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.artifacts_uri == explicit

    def test_no_text_no_files_no_uri_skips_artifact(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        """Existing 'no artifact' path keeps working."""
        eval_id, _, _ = seed_evaluate_task(
            store, artifacts_dir=artifacts_dir
        )
        csrf = get_csrf(signed_in_client)
        _claim(signed_in_client, eval_id, csrf)

        resp = signed_in_client.post(
            f"/evaluator/{eval_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "metric.score": "0.5",
                "artifact_text": "",
                "artifacts_uri": "",
            },
        )
        assert resp.status_code == 200, resp.text
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.artifacts_uri is None
