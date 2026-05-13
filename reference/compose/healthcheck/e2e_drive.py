#!/usr/bin/env python3
"""Phase 10e end-to-end Compose drill — python driver.

Drives a single ideator walkthrough and a single admin-reclaim drill
against the live web-ui container, then exits zero on success or
non-zero with a diagnostic on failure.

Run from ``e2e.sh``; expects these env vars:

- ``EDEN_E2E_WEB_UI_URL`` — base URL of the web-ui (e.g.
  ``http://localhost:8090``)
- ``EDEN_BASE_COMMIT_SHA`` — 40-hex SHA to use as ``parent_commits``
  on the ideator submit (read from setup-experiment's ``.env``)

The flow is documented in
``docs/archive/eden-phase-10e-compose-e2e.md`` §C / §D.
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
    ui: httpx.Client, expected_count: int, deadline_s: float
) -> list[str]:
    """Poll ``/ideator/`` until at least ``expected_count`` ideation tasks are pending.

    Returns the discovered task ids in the order they appear on the
    page. 12a-2 wave 4 replaced the fixed ``ideation-0001..N`` seed
    shape with policy-driven UUID-suffixed ids — the drill must read
    whatever ids the orchestrator's policy produced rather than
    assume specific values.

    The orchestrator has no compose healthcheck so ``compose up --wait``
    can return before policy-driven seeding completes. Polling avoids
    both an arbitrary sleep and a race against slow startup.
    """
    # Match ``/ideator/<task-id>/claim`` action attributes — the
    # ideator list page renders one claim form per pending task and
    # the form's action is uniquely the task id we want.
    claim_re = re.compile(
        r'action="/ideator/(?P<tid>[A-Za-z0-9_.\-]+)/claim"'
    )
    end = time.monotonic() + deadline_s
    last: httpx.Response | None = None
    while time.monotonic() < end:
        resp = ui.get("/ideator/")
        last = resp
        if resp.status_code == 200:
            ids: list[str] = []
            seen: set[str] = set()
            for m in claim_re.finditer(resp.text):
                tid = m.group("tid")
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
            if len(ids) >= expected_count:
                return ids[:expected_count]
        time.sleep(0.5)
    _fail(
        f"orchestrator did not seed >= {expected_count} ideation tasks "
        f"within {deadline_s:.0f}s",
        response=last,
    )
    return []  # unreachable


def _ideator_walkthrough(
    ui: httpx.Client, task_id: str, base_commit_sha: str
) -> None:
    """Claim → draft → submit one ideation task.

    The submit returns 200 with the rendered ``ideator_submitted.html``
    template (NOT a 303 — see chunk-9b
    ``reference/services/web-ui/src/eden_web_ui/routes/ideator.py``).
    """
    # GET /ideator/ to scrape CSRF for the claim form.
    resp = ui.get("/ideator/")
    if resp.status_code != 200:
        _fail("GET /ideator/ failed", response=resp)
    csrf = _scrape_csrf(resp.text, where="GET /ideator/")

    # Claim.
    resp = _form_post(ui, f"/ideator/{task_id}/claim", [("csrf_token", csrf)])
    if resp.status_code != 303:
        _fail(f"claim {task_id} did not 303", response=resp)

    # Draft form. Re-scrape CSRF — the draft renders its own form.
    resp = ui.get(f"/ideator/{task_id}/draft")
    if resp.status_code != 200:
        _fail(f"GET /ideator/{task_id}/draft failed", response=resp)
    csrf = _scrape_csrf(resp.text, where=f"GET /ideator/{task_id}/draft")

    # Submit a single idea row.
    fields = [
        ("csrf_token", csrf),
        ("status", "success"),
        ("slug", "e2e-feat"),
        ("priority", "1.0"),
        ("parent_commits", base_commit_sha),
        ("content", "## why\n\nphase 10e end-to-end drill.\n"),
    ]
    resp = _form_post(ui, f"/ideator/{task_id}/submit", fields)
    if resp.status_code != 200:
        _fail(f"submit {task_id} returned non-200", response=resp)
    if "submitted" not in resp.text.lower():
        _fail(
            f"submit {task_id} 200 but body lacks 'submitted'", response=resp
        )


def _admin_reclaim_drill(ui: httpx.Client, task_id: str) -> None:
    """Claim a ideation task via web UI, do not submit, then admin-reclaim it.

    The admin reclaim redirect URL has a TRAILING SLASH before the
    query (``/admin/tasks/<id>/?reclaimed=ok``) — see chunk-9e
    ``reference/services/web-ui/src/eden_web_ui/routes/admin.py``.
    """
    # Claim.
    resp = ui.get("/ideator/")
    if resp.status_code != 200:
        _fail("GET /ideator/ for admin drill failed", response=resp)
    csrf = _scrape_csrf(resp.text, where="GET /ideator/ (admin drill)")
    resp = _form_post(ui, f"/ideator/{task_id}/claim", [("csrf_token", csrf)])
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


def _dispatch_mode_toggle_drill(ui: httpx.Client) -> None:
    """Flip `integration` to manual and back, verifying the form reflects state.

    The drill goes through the /admin/dispatch-mode/ web-UI route
    rather than the wire endpoint directly so we exercise the
    POST → 303 → re-render path the operator uses. After the flip,
    the form's manual radio for ``integration`` should be checked;
    after flipping back, the auto radio.
    """
    resp = ui.get("/admin/dispatch-mode/")
    if resp.status_code != 200:
        _fail("GET /admin/dispatch-mode/ failed", response=resp)
    csrf = _scrape_csrf(resp.text, where="GET /admin/dispatch-mode/")

    # Flip integration to manual; preserve other keys at auto.
    fields = [
        ("csrf_token", csrf),
        ("ideation_creation", "auto"),
        ("execution_dispatch", "auto"),
        ("evaluation_dispatch", "auto"),
        ("integration", "manual"),
    ]
    resp = _form_post(ui, "/admin/dispatch-mode/", fields)
    if resp.status_code != 303:
        _fail("dispatch-mode flip POST did not 303", response=resp)
    location = resp.headers.get("location", "")
    if "dispatched=ok" not in location:
        _fail(
            f"dispatch-mode flip redirected to {location!r}; "
            "expected 'dispatched=ok'",
            response=resp,
        )

    # Follow the redirect and verify the integration row's manual
    # radio is now checked. The integration row contains
    # ``name="integration"`` and the manual <input> is on the same
    # form line as the ``checked`` marker.
    resp = ui.get("/admin/dispatch-mode/")
    if resp.status_code != 200:
        _fail("post-flip GET /admin/dispatch-mode/ failed", response=resp)
    idx = resp.text.find('name="integration"')
    if idx < 0:
        _fail(
            "dispatch-mode form has no integration row after flip",
            response=resp,
        )
    manual_idx = resp.text.find('value="manual"', idx)
    if manual_idx < 0:
        _fail("dispatch-mode integration row has no manual radio")
    window = resp.text[manual_idx : manual_idx + 200]
    if "checked" not in window:
        _fail(
            "dispatch-mode integration manual radio is not checked after flip"
        )

    # Flip back to auto so the rest of the e2e proceeds normally
    # (integration must run for the variants to reach
    # variant.integrated).
    csrf = _scrape_csrf(resp.text, where="post-flip GET dispatch-mode")
    fields = [
        ("csrf_token", csrf),
        ("ideation_creation", "auto"),
        ("execution_dispatch", "auto"),
        ("evaluation_dispatch", "auto"),
        ("integration", "auto"),
    ]
    resp = _form_post(ui, "/admin/dispatch-mode/", fields)
    if resp.status_code != 303:
        _fail("dispatch-mode flip-back POST did not 303", response=resp)


def _reassign_drill(ui: httpx.Client, task_id: str) -> None:
    """Reassign a pending ideation task and verify the target update.

    Pending reassign emits a single ``task.reassigned`` event. The
    drill posts the form, follows the redirect to confirm the
    success banner, and re-reads the task-detail page to verify the
    target was updated.

    The target is the worker ``ideator-1`` (the headless ideator-host
    container's registered worker_id) so the task can STILL be
    claimed and completed by the headless ideator in stage 2. The
    eligibility ladder will reject claims by any other worker; the
    task being completed at all proves the targeted-claim path
    works end-to-end.
    """
    # GET the reassign form to scrape CSRF.
    resp = ui.get(f"/admin/tasks/{task_id}/reassign")
    if resp.status_code != 200:
        _fail(
            f"GET /admin/tasks/{task_id}/reassign failed", response=resp
        )
    csrf = _scrape_csrf(
        resp.text, where=f"GET /admin/tasks/{task_id}/reassign"
    )
    fields = [
        ("csrf_token", csrf),
        ("target_kind", "worker"),
        ("target_id_worker", "ideator-1"),
        ("reason", "e2e drill route"),
    ]
    resp = _form_post(
        ui, f"/admin/tasks/{task_id}/reassign", fields
    )
    if resp.status_code != 303:
        _fail(
            f"reassign POST {task_id} did not 303", response=resp
        )
    location = resp.headers.get("location", "")
    if "reassigned=ok" not in location:
        _fail(
            f"reassign {task_id} redirected to {location!r}; "
            "expected 'reassigned=ok'",
            response=resp,
        )


def main() -> int:
    """Drive the ideator + admin-reclaim + dispatch-mode + reassign drills end-to-end."""
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

    print(f"e2e_drive: connecting to {web_url}", flush=True)
    try:
        with httpx.Client(base_url=web_url, timeout=15.0) as ui:
            # Sign in (anonymous stub) BEFORE polling — admin/ideator
            # pages redirect to /signin without a session, so an
            # unauthenticated poll loop would just see 303s.
            resp = ui.post("/signin", follow_redirects=False)
            if resp.status_code != 303:
                _fail("POST /signin did not 303", response=resp)

            # 12a-2 wave 7: the orchestrator now uses UUID-suffixed
            # ideation task ids (policy-driven). Discover them
            # dynamically. e2e.sh sets MAX_TOTAL=4 so we poll for
            # exactly 4 pending tasks; the drill claims the first
            # two and leaves the rest for the headless ideator-host
            # stage 2 picks up.
            seeded_ids = _wait_for_seeded_tasks(
                ui, expected_count=4, deadline_s=30.0
            )
            print(
                f"e2e_drive: discovered {len(seeded_ids)} seeded ideation "
                f"tasks: {seeded_ids}",
                flush=True,
            )
            submit_id, reclaim_id = seeded_ids[0], seeded_ids[1]
            reassign_id = seeded_ids[2]

            # 12a-2 wave 7: dispatch-mode toggle drill — flip
            # integration to manual and back, verifying the form
            # reflects state. Must run BEFORE the ideator
            # walkthrough so the manual flip doesn't strand
            # already-success variants without integration. (The
            # flip-back inside the helper restores auto so the rest
            # of the experiment proceeds normally.)
            _dispatch_mode_toggle_drill(ui)
            print(
                "e2e_drive: dispatch-mode toggle drill OK",
                flush=True,
            )

            # 12a-2 wave 7: reassign drill — reassign one pending
            # ideation task to the `admins` group via the admin UI.
            # The headless ideator-host (stage 2) doesn't claim
            # `admins`-targeted tasks, so this id stays pending
            # through the rest of the run; we assert the wire-level
            # task.reassigned event lands via e2e.sh's event-log
            # post-conditions.
            _reassign_drill(ui, reassign_id)
            print(
                f"e2e_drive: reassign drill OK ({reassign_id} reassigned)",
                flush=True,
            )

            _ideator_walkthrough(ui, submit_id, base_commit_sha=base_sha)
            print(
                f"e2e_drive: ideator walkthrough OK ({submit_id} submitted)",
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
