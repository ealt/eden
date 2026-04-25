"""Planner-module routes.

Implements the spec-to-code map pinned in section §C of the Phase
9 plan: list pending plan tasks, claim with a TTL, draft proposals,
submit. Errors propagate as canonical wire-error names so the user
sees an honest banner.

The Phase-3 retry policy on ``submit`` leverages chapter 07 §2.4 /
§8.1: a content-equivalent resubmit returns 200, so transport
failures are retried up to 3 times with exponential backoff before
the orphaned-proposals error page is reached.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from eden_contracts import Proposal
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    IllegalTransition,
    InvalidPrecondition,
    PlanSubmission,
    WrongToken,
)
from eden_storage.submissions import submissions_equivalent
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..artifacts import write_proposal_artifact
from ..forms import ProposalDraft, parse_proposal_rows
from ._helpers import csrf_ok, get_session, htmx_aware_redirect, is_htmx_request

router = APIRouter(prefix="/planner")


# In-memory mapping of (csrf_token, task_id) -> claim token. The CSRF
# token is per-session (rotates on session-secret rotation), so two
# browser sessions with the same configured worker_id cannot reuse
# each other's claims. We deliberately do not persist this across
# restarts — a UI restart drops claim tokens and the TTL sweep
# recovers stranded tasks (eden_dispatch.sweep_expired_claims).
_CLAIMS: dict[tuple[str, str], str] = {}


def _claim_key(session_csrf: str, task_id: str) -> tuple[str, str]:
    return (session_csrf, task_id)


def _list_recent_proposals(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_proposals()
    return items[-limit:]


def _list_recent_trials(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_trials()
    return items[-limit:]


@router.get("/", response_class=HTMLResponse, response_model=None)
async def list_pending(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    pending = store.list_tasks(kind="plan", state="pending")
    config = request.app.state.experiment_config
    return request.app.state.templates.TemplateResponse(
        request,
        "planner_list.html",
        {
            "session": session,
            "pending": pending,
            "objective": config.objective,
            "metrics_schema": config.metrics_schema.root,
            "recent_proposals": _list_recent_proposals(store),
            "recent_trials": _list_recent_trials(store),
            "banner": request.query_params.get("banner"),
        },
    )


@router.post("/{task_id}/claim", response_model=None)
async def claim(
    task_id: str,
    request: Request,
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response(request)
    store = request.app.state.store
    now: Callable[[], Any] = request.app.state.now
    expires_at = now() + timedelta(seconds=request.app.state.claim_ttl_seconds)
    try:
        result = store.claim(task_id, session.worker_id, expires_at=expires_at)
    except (IllegalTransition, InvalidPrecondition) as exc:
        banner = _wire_error_banner(exc)
        return RedirectResponse(url=f"/planner/?banner={banner}", status_code=303)
    _CLAIMS[_claim_key(session.csrf, task_id)] = result.token
    return RedirectResponse(url=f"/planner/{task_id}/draft", status_code=303)


@router.get("/{task_id}/draft", response_class=HTMLResponse, response_model=None)
async def draft_form(
    task_id: str,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    token = _CLAIMS.get(_claim_key(session.csrf, task_id))
    if token is None:
        return RedirectResponse(
            url="/planner/?banner=claim+missing+from+session",
            status_code=303,
        )
    config = request.app.state.experiment_config
    return request.app.state.templates.TemplateResponse(
        request,
        "planner_claim.html",
        {
            "session": session,
            "task_id": task_id,
            "objective": config.objective,
            "metrics_schema": config.metrics_schema.root,
            "errors": None,
            "form_state": [_empty_row()],
            "row_indices": [0],
        },
    )


@router.post("/{task_id}/add_row", response_model=None)
async def add_row(task_id: str, request: Request):
    """Append one empty proposal row to the draft form.

    Two transports, same end state:

    - With JS, htmx posts here with ``HX-Request: true``. We respond
      with the rendered ``_proposal_row.html`` fragment for the new
      row only; htmx swaps it as ``beforeend`` of ``#proposal-rows``
      so the user's existing input is untouched. Redirect/error
      branches send ``HX-Redirect`` on a 204 so htmx does a full
      client-side navigation rather than swapping a redirected page
      into the rows container.
    - Without JS, the same button submits the form normally and we
      re-render the whole ``planner_claim.html`` with the
      collected state plus one more empty row, or 303 to the
      sign-in / planner list on auth/claim failures.
    """
    session = get_session(request)
    if session is None:
        return htmx_aware_redirect(request, "/signin")
    form = await request.form()
    if not csrf_ok(session, form.get("csrf_token")):  # type: ignore[arg-type]
        return _csrf_failure_response(request)
    if _CLAIMS.get(_claim_key(session.csrf, task_id)) is None:
        return htmx_aware_redirect(
            request, "/planner/?banner=claim+missing+from+session"
        )

    if is_htmx_request(request):
        # The htmx-enhanced path: figure out where the new row goes
        # by counting existing rows in the form, then render only
        # the partial. The user's existing rows are already on the
        # page; htmx appends ours to ``#proposal-rows``.
        existing = max(
            len(form.getlist("slug")),
            len(form.getlist("priority")),
            len(form.getlist("parent_commits")),
            len(form.getlist("rationale")),
        )
        return request.app.state.templates.TemplateResponse(
            request,
            "_proposal_row.html",
            {"i": existing, "row_state": _empty_row(), "row_errs": {}},
        )

    config = request.app.state.experiment_config
    slugs = [str(v) for v in form.getlist("slug")]
    priorities = [str(v) for v in form.getlist("priority")]
    parents = [str(v) for v in form.getlist("parent_commits")]
    rationales = [str(v) for v in form.getlist("rationale")]
    state = _form_state_from_inputs(slugs, priorities, parents, rationales)
    state.append(_empty_row())
    return request.app.state.templates.TemplateResponse(
        request,
        "planner_claim.html",
        {
            "session": session,
            "task_id": task_id,
            "objective": config.objective,
            "metrics_schema": config.metrics_schema.root,
            "errors": None,
            "form_state": state,
            "row_indices": list(range(len(state))),
        },
    )


@router.post("/{task_id}/submit", response_model=None)
async def submit_plan(task_id: str, request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    form = await request.form()
    if not csrf_ok(session, form.get("csrf_token")):  # type: ignore[arg-type]
        return _csrf_failure_response(request)
    status = form.get("status")
    if status not in ("success", "error"):
        return _bad_request("invalid submit status")

    token = _CLAIMS.get(_claim_key(session.csrf, task_id))
    if token is None:
        return RedirectResponse(
            url="/planner/?banner=claim+missing+from+session",
            status_code=303,
        )

    store = request.app.state.store
    config = request.app.state.experiment_config

    if status == "error":
        try:
            store.submit(task_id, token, PlanSubmission(status="error"))
        except DispatchError as exc:
            return _render_error(request, _wire_error_banner(exc))
        _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
        return _render_submitted(request, task_id, status="error", proposal_ids=())

    slugs = [str(v) for v in form.getlist("slug")]
    priorities = [str(v) for v in form.getlist("priority")]
    parents = [str(v) for v in form.getlist("parent_commits")]
    rationales = [str(v) for v in form.getlist("rationale")]
    drafts, errors = parse_proposal_rows(slugs, priorities, parents, rationales)
    if errors:
        return request.app.state.templates.TemplateResponse(
            request,
            "planner_claim.html",
            {
                "session": session,
                "task_id": task_id,
                "objective": config.objective,
                "metrics_schema": config.metrics_schema.root,
                "errors": errors,
                "form_state": _form_state_from_inputs(slugs, priorities, parents, rationales),
                "row_indices": list(range(max(1, len(slugs) or 1))),
            },
            status_code=400,
        )

    proposal_ids: list[str] = []
    artifacts_dir = request.app.state.artifacts_dir
    now: Callable[[], Any] = request.app.state.now

    # Phase 1: write artifacts + create proposals as drafting.
    for draft in drafts:
        proposal_id = uuid.uuid4().hex
        artifacts_uri = write_proposal_artifact(
            artifacts_dir, proposal_id, draft.rationale
        )
        proposal = _make_proposal(
            proposal_id=proposal_id,
            experiment_id=request.app.state.experiment_id,
            draft=draft,
            artifacts_uri=artifacts_uri,
            now_iso=_iso(now()),
        )
        try:
            store.create_proposal(proposal)
        except DispatchError as exc:
            return _render_error(
                request,
                f"create_proposal failed for {proposal_id}: {_wire_error_banner(exc)}",
            )
        proposal_ids.append(proposal_id)

    # Phase 2: flip to ready.
    for proposal_id in proposal_ids:
        try:
            store.mark_proposal_ready(proposal_id)
        except DispatchError as exc:
            return _render_error(
                request,
                f"mark_proposal_ready failed for {proposal_id}: {_wire_error_banner(exc)}",
            )

    # Phase 3: submit, with retry-before-orphan.
    submission = PlanSubmission(status="success", proposal_ids=tuple(proposal_ids))
    ok, banner = _retry_submit(store, task_id, token, submission)
    if not ok:
        return _render_orphaned(request, task_id, proposal_ids, banner=banner)

    _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
    return _render_submitted(
        request, task_id, status="success", proposal_ids=tuple(proposal_ids)
    )


_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


def _retry_submit(
    store: Any, task_id: str, token: str, submission: PlanSubmission
) -> tuple[bool, str | None]:
    """Resubmit on transport-only failures up to 3 times.

    Per chapter 07 §2.4 / §8.1, a content-equivalent resubmit is
    idempotent. Returns ``(ok, banner)``:

    - ``(True, None)`` on success (including a same-content
      resubmit that returns 200).
    - ``(False, banner)`` on a definitive divergent response or
      after 3 indeterminate failures. ``banner`` is the canonical
      ``eden://error/<name>`` (or a transport summary) suitable for
      surfacing to the user.

    Exception classification (per
    ``feedback_retry_readback_test_mocks.md`` rule 1):

    - ``WrongToken``, ``ConflictingResubmission``, and
      ``InvalidPrecondition`` are definitive short-circuits;
      retrying them cannot change the outcome.
    - ``IllegalTransition`` is **not** definitive: it falls
      through to a read-back that distinguishes "we won; the
      orchestrator already terminalized" (success) from "we
      lost; task reclaimed or different submission won"
      (orphan). Treating it as a definitive orphan would
      mis-classify a successful submit whose response was lost.
    - Other exceptions (transport-shaped) are retried with
      backoff; on exhaustion, the read-back resolves.

    On any definitive failure, the caller renders the orphan page
    rather than a "preserve input + claim again" form. Reasoning:
    by the time Phase 3 runs, Phase 1 has already created
    proposals and Phase 2 has marked them ready. The work product
    that needs operator recovery is the orphaned proposals
    themselves (whose IDs the orphan page lists), not the form
    inputs. Form-input preservation applies to *validation*
    errors, where no store mutation has happened yet.
    """
    last_exc: BaseException | None = None
    needs_readback = False
    for delay in _RETRY_DELAYS_S:
        try:
            store.submit(task_id, token, submission)
            return True, None
        except (WrongToken, ConflictingResubmission, InvalidPrecondition) as exc:
            return False, _wire_error_banner(exc)
        except IllegalTransition as exc:
            last_exc = exc
            needs_readback = True
            break
        except Exception as exc:  # noqa: BLE001 — transport-shaped or unknown
            last_exc = exc
            time.sleep(delay)

    # Either retries exhausted with transport-shape failures, or
    # IllegalTransition broke us out of the loop. Either way,
    # read-back disambiguates: an equivalent prior submission means
    # we won and the response was lost in transit; anything else is
    # orphan.
    if needs_readback or last_exc is not None:
        try:
            task = store.read_task(task_id)
        except Exception as exc:  # noqa: BLE001
            return (
                False,
                "transport failure after retries; "
                f"read-back failed: {exc.__class__.__name__}",
            )
        if task.state in {"submitted", "completed", "failed"}:
            try:
                prior = store.read_submission(task_id)
            except Exception as exc:  # noqa: BLE001
                return (
                    False,
                    "transport failure after retries; "
                    f"read-submission failed: {exc.__class__.__name__}",
                )
            if prior is not None and submissions_equivalent(prior, submission):
                # We won — the orchestrator already accepted (or
                # rejected) our equivalent prior submission and the
                # transport just lost the response.
                return True, None
            # state moved past submitted with a different submission
            # → orphan conflict.
            return False, "eden://error/conflicting-resubmission"
        last_name = last_exc.__class__.__name__ if last_exc else "unknown"
        return False, f"transport failure after retries: {last_name}"

    return True, None


def _make_proposal(
    *,
    proposal_id: str,
    experiment_id: str,
    draft: ProposalDraft,
    artifacts_uri: str,
    now_iso: str,
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        experiment_id=experiment_id,
        slug=draft.slug,
        priority=draft.priority,
        parent_commits=list(draft.parent_commits),
        artifacts_uri=artifacts_uri,
        state="drafting",
        created_at=now_iso,
    )


def _iso(dt: Any) -> str:
    """Emit Zulu-suffixed ISO 8601: ``YYYY-MM-DDTHH:MM:SS(.sss)?Z``."""
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[: -len("+00:00")] + "Z"
    return s


def _empty_row() -> dict[str, str]:
    return {"slug": "", "priority": "1.0", "parent_commits": "", "rationale": ""}


def _form_state_from_inputs(
    slugs: list[str], priorities: list[str], parents: list[str], rationales: list[str]
) -> list[dict[str, str]]:
    n = max(len(slugs), len(priorities), len(parents), len(rationales))
    out: list[dict[str, str]] = []
    for i in range(n):
        out.append(
            {
                "slug": slugs[i] if i < len(slugs) else "",
                "priority": priorities[i] if i < len(priorities) else "1.0",
                "parent_commits": parents[i] if i < len(parents) else "",
                "rationale": rationales[i] if i < len(rationales) else "",
            }
        )
    return out


def _render_submitted(
    request: Request,
    task_id: str,
    *,
    status: str,
    proposal_ids: tuple[str, ...],
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "planner_submitted.html",
        {
            "task_id": task_id,
            "status": status,
            "proposal_ids": proposal_ids,
        },
    )


def _render_orphaned(
    request: Request,
    task_id: str,
    proposal_ids: list[str],
    *,
    banner: str | None = None,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "planner_orphaned.html",
        {
            "task_id": task_id,
            "proposal_ids": proposal_ids,
            "banner": banner,
        },
        status_code=502,
    )


def _render_error(request: Request, message: str) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "_error.html",
        {"title": "Wire error", "message": message},
        status_code=502,
    )


def _csrf_failure_response(request: Request | None = None) -> HTMLResponse:
    """Reject the request with 403.

    For htmx requests we additionally set ``HX-Reswap: none`` so htmx
    does not swap the error body into the configured target (e.g.
    ``#proposal-rows``). Adding the same ``HX-Trigger`` header is
    intentionally skipped — chunk 1 has no client-side error toast
    yet; 9e revisits live error UX.
    """
    headers: dict[str, str] = {}
    if request is not None and is_htmx_request(request):
        headers["hx-reswap"] = "none"
    return HTMLResponse(
        content="CSRF token missing or invalid",
        status_code=403,
        headers=headers,
    )


def _bad_request(message: str) -> HTMLResponse:
    return HTMLResponse(content=message, status_code=400)


_ERROR_NAMES: dict[type, str] = {
    WrongToken: "eden://error/wrong-token",
    IllegalTransition: "eden://error/illegal-transition",
    ConflictingResubmission: "eden://error/conflicting-resubmission",
    InvalidPrecondition: "eden://error/invalid-precondition",
}


def _wire_error_banner(exc: BaseException) -> str:
    name = _ERROR_NAMES.get(type(exc))
    if name is None:
        return f"unexpected error: {exc.__class__.__name__}"
    return name
