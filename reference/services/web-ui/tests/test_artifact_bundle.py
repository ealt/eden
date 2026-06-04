"""Web-UI route-level artifact-serving security tests (issue #120 / #168).

The artifact writer + bundle helpers themselves moved to
``eden_service_common`` in issue #168; their unit coverage lives in
``reference/services/_common/tests/test_artifacts.py``. What stays here is the
web-UI-specific concern: the ``/artifacts?...&entry=`` serve route must not let
a crafted ``entry=`` escape the bundle.
"""

from __future__ import annotations

from eden_web_ui.artifacts import (
    UploadedFile,
    submission_naming,
    write_artifact_bundle,
)


class TestBundleEntryServingSecurity:
    """Issue #120: the ``?entry=`` query param must not escape the bundle."""

    def _setup_signed_in(self, store, artifacts_dir):
        from datetime import UTC, datetime

        from conftest import (
            EXPERIMENT_ID,
            SESSION_SECRET,
            _config,
            _one_experiment_factory,
            web_ui_worker_id,
        )
        from eden_web_ui import make_app
        from fastapi.testclient import TestClient

        app = make_app(
            store_factory=_one_experiment_factory(store, admin_store=store),
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=web_ui_worker_id(store),
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=False,
            now=lambda: datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        )
        c = TestClient(app)
        c.post("/signin", follow_redirects=False)
        return c

    def _make_bundle(self, artifacts_dir):
        write_artifact_bundle(
            artifacts_dir,
            submission_naming("bundle-x", headline="content.md"),
            text_content="headline",
            uploads=[
                UploadedFile(
                    filename="payload.txt", data=b"safe", content_type=None
                )
            ],
        )
        return artifacts_dir / "bundle-x.tar.gz"

    def test_entry_slash_rejected(self, store, artifacts_dir) -> None:
        from urllib.parse import quote
        self._make_bundle(artifacts_dir)
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{(artifacts_dir / 'bundle-x.tar.gz').resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=a/b"
        )
        assert resp.status_code == 400

    def test_entry_dotdot_rejected(self, store, artifacts_dir) -> None:
        from urllib.parse import quote
        self._make_bundle(artifacts_dir)
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{(artifacts_dir / 'bundle-x.tar.gz').resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=.."
        )
        assert resp.status_code == 400

    def test_entry_param_on_non_bundle_rejected(
        self, store, artifacts_dir
    ) -> None:
        from urllib.parse import quote
        target = artifacts_dir / "plain.md"
        target.write_text("not a bundle")
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{target.resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=anything.txt"
        )
        assert resp.status_code == 400

    def test_missing_entry_returns_404(self, store, artifacts_dir) -> None:
        from urllib.parse import quote
        self._make_bundle(artifacts_dir)
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{(artifacts_dir / 'bundle-x.tar.gz').resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=does-not-exist.txt"
        )
        assert resp.status_code == 404
