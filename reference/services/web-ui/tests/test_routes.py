"""Per-route unit tests against ``make_app`` with an in-memory store."""

from __future__ import annotations

from conftest import get_csrf
from fastapi.testclient import TestClient


class TestAuth:
    def test_root_redirects_to_signin_without_session(
        self, client: TestClient
    ) -> None:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_signin_form_renders(self, client: TestClient) -> None:
        resp = client.get("/signin")
        assert resp.status_code == 200
        assert "continue as" in resp.text.lower()

    def test_signin_post_sets_session_cookie(self, client: TestClient) -> None:
        resp = client.post("/signin", follow_redirects=False)
        assert resp.status_code == 303
        cookie_header = resp.headers.get("set-cookie", "")
        assert "eden_web_ui_session=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=lax" in cookie_header
        assert "Secure" not in cookie_header  # default secure_cookies=False
        assert "Path=/" in cookie_header

    def test_signout_clears_cookie(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post("/signout", follow_redirects=False)
        assert resp.status_code == 303
        # delete_cookie sets max-age=0 / expires-in-the-past
        cookie_header = resp.headers.get("set-cookie", "")
        assert "eden_web_ui_session=" in cookie_header


class TestIndex:
    def test_signed_in_renders_counts(
        self, signed_in_client: TestClient, store
    ) -> None:
        store.create_plan_task("t-1")
        store.create_plan_task("t-2")
        resp = signed_in_client.get("/")
        assert resp.status_code == 200
        # rough check: counts cell should reflect 2 pending plan tasks
        assert "<td>plan</td><td>2</td>" in resp.text.replace(" ", "").replace(
            "\n", ""
        )


class TestPlannerList:
    def test_renders_objective_and_metrics_schema(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/planner/")
        assert resp.status_code == 200
        # Objective from the fixture is {expr: "score", direction: "maximize"};
        # rendering relies on ObjectiveSpec's str-form. Just check the metric.
        assert "score" in resp.text
        assert "real" in resp.text

    def test_lists_pending_tasks(
        self, signed_in_client: TestClient, store
    ) -> None:
        store.create_plan_task("t-1")
        resp = signed_in_client.get("/planner/")
        assert "t-1" in resp.text


class TestCSRF:
    def test_claim_without_csrf_rejected(
        self, signed_in_client: TestClient, store
    ) -> None:
        store.create_plan_task("t-1")
        resp = signed_in_client.post("/planner/t-1/claim", data={})
        assert resp.status_code == 403

    def test_claim_with_wrong_csrf_rejected(
        self, signed_in_client: TestClient, store
    ) -> None:
        store.create_plan_task("t-1")
        resp = signed_in_client.post(
            "/planner/t-1/claim", data={"csrf_token": "not-the-real-token"}
        )
        assert resp.status_code == 403

    def test_claim_with_correct_csrf_succeeds(
        self, signed_in_client: TestClient, store
    ) -> None:
        store.create_plan_task("t-1")
        token = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/planner/t-1/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/planner/t-1/draft"
        assert store.read_task("t-1").state == "claimed"
