#!/usr/bin/env python3
"""Phase 10e end-to-end Compose drill — python driver.

Drives a single planner walkthrough and a single admin-reclaim drill
against the live web-ui container, then exits zero on success or
non-zero with a diagnostic on failure.

Run from ``e2e.sh``; expects these env vars:

- ``EDEN_E2E_WEB_UI_URL`` — base URL of the web-ui (e.g.
  ``http://localhost:8090``)
- ``EDEN_BASE_COMMIT_SHA`` — 40-hex SHA to use as ``parent_commits``
  on the planner submit (read from setup-experiment's ``.env``)

The flow is documented in
``docs/plans/eden-phase-10e-compose-e2e.md`` §C / §D.
"""

from __future__ import annotations

import os
import re
import sys
import time
from urllib.parse import urlencode

import httpx

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def _fail(msg: str, *, response: httpx.Response | None = None) -> None:
    """Print a diagnostic to stderr and exit non-zero."""
    print(f"e2e_drive: {msg}", file=sys.stderr)
    if response is not None:
        print(f"  status: {response.status_code}", file=sys.stderr)
        body = response.text
        excerpt = body if len(body) <= 800 else body[:800] + "…"
        print(f"  body: {excerpt}", file=sys.stderr)
    sys.exit(1)


def _scrape_csrf(html: str, *, where: str) -> str:
    m = _CSRF_RE.search(html)
    if m is None:
        _fail(f"could not find csrf_token in {where} response body")
        # _fail exits; this return is unreachable but appeases the type checker
        return ""
    return m.group(1)


def _form_post(
    ui: httpx.Client, path: str, fields: list[tuple[str, str]]
) -> httpx.Response:
    return ui.post(
        path,
        content=urlencode(fields),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def _wait_for_seeded_tasks(
    ui: httpx.Client, ids: tuple[str, ...], deadline_s: float
) -> None:
    """Poll ``/planner/`` until all ``ids`` are visible.

    The orchestrator has no compose healthcheck so ``compose up --wait``
    can return before seeding completes. Polling avoids both an
    arbitrary sleep and a race against slow startup.
    """
    end = time.monotonic() + deadline_s
    last: httpx.Response | None = None
    while time.monotonic() < end:
        resp = ui.get("/planner/")
        last = resp
        if resp.status_code == 200 and all(tid in resp.text for tid in ids):
            return
        time.sleep(0.5)
    _fail(
        f"orchestrator did not seed all tasks {ids} within "
        f"{deadline_s:.0f}s",
        response=last,
    )


def _planner_walkthrough(
    ui: httpx.Client, task_id: str, base_commit_sha: str
) -> None:
    """Claim → draft → submit one plan task.

    The submit returns 200 with the rendered ``planner_submitted.html``
    template (NOT a 303 — see chunk-9b
    ``reference/services/web-ui/src/eden_web_ui/routes/planner.py``).
    """
    # GET /planner/ to scrape CSRF for the claim form.
    resp = ui.get("/planner/")
    if resp.status_code != 200:
        _fail("GET /planner/ failed", response=resp)
    csrf = _scrape_csrf(resp.text, where="GET /planner/")

    # Claim.
    resp = _form_post(ui, f"/planner/{task_id}/claim", [("csrf_token", csrf)])
    if resp.status_code != 303:
        _fail(f"claim {task_id} did not 303", response=resp)

    # Draft form. Re-scrape CSRF — the draft renders its own form.
    resp = ui.get(f"/planner/{task_id}/draft")
    if resp.status_code != 200:
        _fail(f"GET /planner/{task_id}/draft failed", response=resp)
    csrf = _scrape_csrf(resp.text, where=f"GET /planner/{task_id}/draft")

    # Submit a single proposal row.
    fields = [
        ("csrf_token", csrf),
        ("status", "success"),
        ("slug", "e2e-feat"),
        ("priority", "1.0"),
        ("parent_commits", base_commit_sha),
        ("rationale", "## why\n\nphase 10e end-to-end drill.\n"),
    ]
    resp = _form_post(ui, f"/planner/{task_id}/submit", fields)
    if resp.status_code != 200:
        _fail(f"submit {task_id} returned non-200", response=resp)
    if "submitted" not in resp.text.lower():
        _fail(
            f"submit {task_id} 200 but body lacks 'submitted'", response=resp
        )


def _admin_reclaim_drill(ui: httpx.Client, task_id: str) -> None:
    """Claim a plan task via web UI, do not submit, then admin-reclaim it.

    The admin reclaim redirect URL has a TRAILING SLASH before the
    query (``/admin/tasks/<id>/?reclaimed=ok``) — see chunk-9e
    ``reference/services/web-ui/src/eden_web_ui/routes/admin.py``.
    """
    # Claim.
    resp = ui.get("/planner/")
    if resp.status_code != 200:
        _fail("GET /planner/ for admin drill failed", response=resp)
    csrf = _scrape_csrf(resp.text, where="GET /planner/ (admin drill)")
    resp = _form_post(ui, f"/planner/{task_id}/claim", [("csrf_token", csrf)])
    if resp.status_code != 303:
        _fail(
            f"admin-drill claim {task_id} did not 303", response=resp
        )

    # Confirm it shows up in the admin claimed list.
    resp = ui.get("/admin/tasks/?state=claimed")
    if resp.status_code != 200:
        _fail("GET /admin/tasks/?state=claimed failed", response=resp)
    if task_id not in resp.text:
        _fail(
            f"{task_id} not visible in /admin/tasks/?state=claimed",
            response=resp,
        )

    # Re-scrape CSRF from the task-detail page (its own form render).
    resp = ui.get(f"/admin/tasks/{task_id}/")
    if resp.status_code != 200:
        _fail(f"GET /admin/tasks/{task_id}/ failed", response=resp)
    csrf = _scrape_csrf(resp.text, where=f"GET /admin/tasks/{task_id}/")

    # Reclaim.
    resp = _form_post(
        ui, f"/admin/tasks/{task_id}/reclaim", [("csrf_token", csrf)]
    )
    if resp.status_code != 303:
        _fail(
            f"admin reclaim {task_id} did not 303", response=resp
        )
    expected = f"/admin/tasks/{task_id}/?reclaimed=ok"
    location = resp.headers.get("location", "")
    if location != expected:
        _fail(
            f"admin reclaim {task_id} redirected to {location!r}; "
            f"expected {expected!r}",
            response=resp,
        )


def main() -> int:
    """Drive the planner walkthrough and admin-reclaim drill end-to-end."""
    web_url = os.environ.get("EDEN_E2E_WEB_UI_URL")
    base_sha = os.environ.get("EDEN_BASE_COMMIT_SHA")
    if not web_url:
        _fail("EDEN_E2E_WEB_UI_URL not set in environment")
    if not base_sha:
        _fail("EDEN_BASE_COMMIT_SHA not set in environment")
    # _fail exits non-zero, but the type checker doesn't know that;
    # narrow with two separate asserts (ruff PT018 — one assertion per
    # condition).
    assert web_url is not None  # noqa: S101 — type narrowing only
    assert base_sha is not None  # noqa: S101 — type narrowing only

    seeded_ids = tuple(f"plan-{i:04d}" for i in range(1, 5))
    submit_id = "plan-0001"
    reclaim_id = "plan-0002"

    print(f"e2e_drive: connecting to {web_url}", flush=True)
    try:
        with httpx.Client(base_url=web_url, timeout=15.0) as ui:
            # Sign in (anonymous stub) BEFORE polling — admin/planner
            # pages redirect to /signin without a session, so an
            # unauthenticated poll loop would just see 303s.
            resp = ui.post("/signin", follow_redirects=False)
            if resp.status_code != 303:
                _fail("POST /signin did not 303", response=resp)

            # Wait for the orchestrator's seed to land. Polls /planner/
            # — both /planner/ and /admin/tasks/ are auth-gated, so
            # this only works after sign-in.
            _wait_for_seeded_tasks(ui, seeded_ids, deadline_s=30.0)
            print(
                f"e2e_drive: all {len(seeded_ids)} seeded plan tasks "
                "visible",
                flush=True,
            )

            _planner_walkthrough(ui, submit_id, base_commit_sha=base_sha)
            print(
                f"e2e_drive: planner walkthrough OK ({submit_id} submitted)",
                flush=True,
            )

            _admin_reclaim_drill(ui, reclaim_id)
            print(
                f"e2e_drive: admin-reclaim drill OK ({reclaim_id} reclaimed)",
                flush=True,
            )
    except httpx.HTTPError as exc:
        # ConnectTimeout / ConnectError / ReadTimeout / etc. — surface
        # as a concise diagnostic rather than a Python traceback.
        _fail(f"httpx transport error talking to web-ui: {exc!r}")

    print("e2e_drive: PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
