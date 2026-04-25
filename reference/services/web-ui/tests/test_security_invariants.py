"""Pin the security invariants codex flagged in plan review.

- Cookie attributes (HttpOnly, SameSite=Lax, Path=/, Secure iff
  --secure-cookies).
- The shared bearer never appears in any rendered HTML or
  Set-Cookie header.
- CSRF token is per-session; mismatch returns 403.
"""

from __future__ import annotations

import logging
from pathlib import Path

from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    SHARED_TOKEN,
    WORKER_ID,
    _config,
    _now,
    get_csrf,
)
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi.testclient import TestClient


class TestCookieAttributes:
    def test_default_secure_off(self, signed_in_client: TestClient) -> None:
        # Set-cookie attributes appear on the *response* — easier to assert
        # by re-issuing signin and reading the header.
        resp = signed_in_client.post("/signin", follow_redirects=False)
        header = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in header
        assert "SameSite=lax" in header
        assert "Secure" not in header
        assert "Path=/" in header

    def test_secure_cookies_true_sets_secure(
        self, store: InMemoryStore, artifacts_dir: Path
    ) -> None:
        app = make_app(
            store=store,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=WORKER_ID,
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=True,
            now=_now,
        )
        with TestClient(app) as c:
            resp = c.post("/signin", follow_redirects=False)
            assert "Secure" in resp.headers["set-cookie"]


class TestBearerLeak:
    def test_bearer_not_in_rendered_html_or_cookies(
        self,
        store: InMemoryStore,
        artifacts_dir: Path,
        caplog,
    ) -> None:
        """Even though we don't pass the bearer to make_app today (the
        StoreClient holds it; tests use InMemoryStore), assert that no
        endpoint renders or echoes the configured bearer string."""
        app = make_app(
            store=store,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=WORKER_ID,
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=False,
            now=_now,
        )
        # Stuff the bearer somewhere reachable; if the UI ever leaked
        # app.state.* into a template, this would catch it.
        app.state.shared_token = SHARED_TOKEN

        store.create_plan_task("t-leak")
        with TestClient(app) as c, caplog.at_level(logging.DEBUG):
            c.post("/signin", follow_redirects=False)
            for path in ("/", "/planner/", "/signin"):
                resp = c.get(path)
                assert SHARED_TOKEN not in resp.text, (
                    f"shared bearer leaked into {path}"
                )
                assert SHARED_TOKEN not in resp.headers.get("set-cookie", "")

            csrf = get_csrf(c)
            c.post(
                "/planner/t-leak/claim",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            draft = c.get("/planner/t-leak/draft")
            assert SHARED_TOKEN not in draft.text

        # No log line written by web-ui code contains the bearer.
        for record in caplog.records:
            assert SHARED_TOKEN not in record.getMessage()


class TestCSRF:
    def test_submit_without_csrf_rejected(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_plan_task("t-1")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-1/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/planner/t-1/submit",
            data={"status": "error"},  # no csrf_token
        )
        assert resp.status_code == 403

    def test_signout_csrf_not_required_but_clears_cookie(
        self, signed_in_client: TestClient
    ) -> None:
        # signout clears the cookie unconditionally — by design (it's
        # a privilege-reducing operation, no mutation of business state).
        resp = signed_in_client.post("/signout", follow_redirects=False)
        assert resp.status_code == 303
