"""Implementer-module routes.

Implements the spec-to-code map pinned in §C of the Phase 9c plan.
The flow is: list pending implement tasks → claim with TTL +
server-owned trial_id → render draft form (read-only proposal
context, optional inline rationale) → submit, which runs

1. validate the form (slug pattern, hex commit_sha when status=success)
2. (status=success only) check the §3.3 reachability invariants
   against the bare repo: commit_exists + is_ancestor(parent, commit_sha)
3. (status=success only) Pre-Phase-1 ref-collision guard
4. Phase 1: ``store.create_trial`` (status=starting; no commit_sha)
5. Phase 2 (status=success only): ``repo.create_ref(work/<branch>, sha)``
6. Phase 3: ``store.submit`` with retry-before-orphan plus a
   committed-state read-back that keys off
   ``submissions_equivalent`` rather than worker_id

trial_id is generated server-side at claim time and stored in
``_CLAIMS``; it never appears in the request surface.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from eden_contracts import Proposal, Trial
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    IllegalTransition,
    ImplementSubmission,
    InvalidPrecondition,
    WrongToken,
)
from eden_storage.submissions import submissions_equivalent
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..forms import parse_implement_form
from ._helpers import (
    csrf_ok,
    get_session,
    is_htmx_request,
    read_proposal_rationale,
)

router = APIRouter(prefix="/implementer")


# In-memory mapping of (csrf_token, task_id) -> (claim token, trial_id).
# Keyed by session.csrf so two browser sessions on the same configured
# worker_id cannot hijack each other's claims, and so the trial_id
# stays server-side (never round-trips through the request).
_CLAIMS: dict[tuple[str, str], tuple[str, str]] = {}


def _repo_has_origin(repo: Any) -> bool:
    """Return True if the GitRepo has an origin remote configured."""
    try:
        result = repo._run(["remote"], check=False)
    except Exception:  # noqa: BLE001
        return False
    return "origin" in result.stdout.split()


def _claim_key(session_csrf: str, task_id: str) -> tuple[str, str]:
    return (session_csrf, task_id)


def _new_trial_id() -> str:
    return f"trial-{uuid.uuid4().hex[:12]}"


def _branch_name(slug: str, trial_id: str) -> str:
    return f"work/{slug}-{trial_id}"


def _list_recent_trials(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_trials()
    return items[-limit:]


@router.get("/", response_class=HTMLResponse, response_model=None)
async def list_pending(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    pending = store.list_tasks(kind="implement", state="pending")
    config = request.app.state.experiment_config
    return request.app.state.templates.TemplateResponse(
        request,
        "implementer_list.html",
        {
            "session": session,
            "pending": pending,
            "objective": config.objective,
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
        return RedirectResponse(
            url=f"/implementer/?banner={banner}", status_code=303
        )
    trial_id = _new_trial_id()
    _CLAIMS[_claim_key(session.csrf, task_id)] = (result.token, trial_id)
    return RedirectResponse(url=f"/implementer/{task_id}/draft", status_code=303)


@router.get("/{task_id}/draft", response_class=HTMLResponse, response_model=None)
async def draft_form(
    task_id: str,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    entry = _CLAIMS.get(_claim_key(session.csrf, task_id))
    if entry is None:
        return RedirectResponse(
            url="/implementer/?banner=claim+missing+from+session",
            status_code=303,
        )
    _, trial_id = entry
    store = request.app.state.store
    try:
        task = store.read_task(task_id)
        proposal: Proposal = store.read_proposal(task.payload.proposal_id)
    except DispatchError as exc:
        return _render_error(request, _wire_error_banner(exc))
    return _render_draft(
        request,
        session=session,
        task_id=task_id,
        proposal=proposal,
        trial_id=trial_id,
        form_state=_empty_form_state(),
        errors=None,
        status_code=200,
    )


@router.post("/{task_id}/submit", response_model=None)
async def submit(  # noqa: PLR0911 — flow has many distinct outcome arms by design
    task_id: str,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    form = await request.form()
    if not csrf_ok(session, form.get("csrf_token")):  # type: ignore[arg-type]
        return _csrf_failure_response(request)

    entry = _CLAIMS.get(_claim_key(session.csrf, task_id))
    if entry is None:
        return RedirectResponse(
            url="/implementer/?banner=claim+missing+from+session",
            status_code=303,
        )
    token, trial_id = entry

    store = request.app.state.store
    repo = request.app.state.repo
    try:
        task = store.read_task(task_id)
        proposal = store.read_proposal(task.payload.proposal_id)
    except DispatchError as exc:
        return _render_error(request, _wire_error_banner(exc))

    status_raw = str(form.get("status") or "")
    commit_sha_raw = str(form.get("commit_sha") or "")
    description_raw = str(form.get("description") or "")
    draft, errors = parse_implement_form(
        status_raw=status_raw,
        commit_sha_raw=commit_sha_raw,
        description_raw=description_raw,
    )
    form_state = {
        "status": status_raw or "success",
        "commit_sha": commit_sha_raw,
        "description": description_raw,
    }
    if draft is None:
        return _render_draft(
            request,
            session=session,
            task_id=task_id,
            proposal=proposal,
            trial_id=trial_id,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )

    branch = _branch_name(proposal.slug, trial_id)

    if draft.status == "success":
        assert draft.commit_sha is not None
        # §C reachability: commit must exist and descend from every parent.
        if not repo.commit_exists(draft.commit_sha):
            errors.add(
                0,
                "commit_sha",
                f"commit {draft.commit_sha!r} not found in the bare repo; did you push it?",
            )
            return _render_draft(
                request,
                session=session,
                task_id=task_id,
                proposal=proposal,
                trial_id=trial_id,
                form_state=form_state,
                errors=errors,
                status_code=400,
            )
        for parent in proposal.parent_commits:
            if not repo.is_ancestor(parent, draft.commit_sha):
                errors.add(
                    0,
                    "commit_sha",
                    f"commit {draft.commit_sha!r} does not descend from declared parent {parent!r}",
                )
                return _render_draft(
                    request,
                    session=session,
                    task_id=task_id,
                    proposal=proposal,
                    trial_id=trial_id,
                    form_state=form_state,
                    errors=errors,
                    status_code=400,
                )
        # Pre-Phase-1 ref-collision guard.
        if repo.ref_exists(f"refs/heads/{branch}"):
            errors.add(
                0,
                "commit_sha",
                (
                    f"branch refs/heads/{branch} already exists; "
                    "reclaim or pick a different proposal slug"
                ),
            )
            return _render_draft(
                request,
                session=session,
                task_id=task_id,
                proposal=proposal,
                trial_id=trial_id,
                form_state=form_state,
                errors=errors,
                status_code=400,
            )

    now: Callable[[], Any] = request.app.state.now
    started_at = _iso(now())

    # Phase 1: create_trial as starting.
    trial_kwargs: dict[str, Any] = {
        "trial_id": trial_id,
        "experiment_id": request.app.state.experiment_id,
        "proposal_id": proposal.proposal_id,
        "status": "starting",
        "parent_commits": list(proposal.parent_commits),
        "branch": branch,
        "started_at": started_at,
    }
    if draft.description is not None:
        trial_kwargs["description"] = draft.description
    trial = Trial(**trial_kwargs)
    try:
        store.create_trial(trial)
    except DispatchError as exc:
        return _render_error(
            request,
            f"create_trial failed for {trial_id}: {_wire_error_banner(exc)}",
        )
    except Exception as exc:  # noqa: BLE001 — transport-shaped
        # The trial may or may not have committed on the server. The
        # orphan recovery for a stranded `starting` trial is the same
        # whether the server saw the request or not (TTL → reclaim →
        # trial → error), so render the orphan page with an
        # indeterminate banner rather than crashing.
        return _render_orphaned(
            request,
            task_id=task_id,
            trial_id=trial_id,
            commit_sha=draft.commit_sha,
            branch=branch if draft.status == "success" else None,
            banner=f"create_trial transport failure: {exc.__class__.__name__}",
            recovery_kind="transport",
        )

    # Phase 2: create_ref locally (status=success only). Phase 10d
    # follow-up B §D.7: when origin is configured, also push_ref so
    # the orchestrator's clone can fetch the work/* commit. Push
    # failure rolls back the local ref + classifies as orphan.
    if draft.status == "success":
        assert draft.commit_sha is not None
        try:
            repo.create_ref(f"refs/heads/{branch}", draft.commit_sha)
        except Exception as exc:  # noqa: BLE001 — git/transport-shaped
            return _render_orphaned(
                request,
                task_id=task_id,
                trial_id=trial_id,
                commit_sha=draft.commit_sha,
                branch=branch,
                banner=f"repo error: {exc.__class__.__name__}",
                recovery_kind="auto",
            )
        if _repo_has_origin(repo):
            try:
                repo.push_ref(f"refs/heads/{branch}")
            except Exception as exc:  # noqa: BLE001
                # Roll back local ref so we don't leave a local-only
                # work/* the orchestrator can never integrate. The
                # next host startup's fetch_all_heads --prune is the
                # backstop if delete_ref itself fails here.
                import contextlib
                with contextlib.suppress(Exception):
                    repo.delete_ref(
                        f"refs/heads/{branch}",
                        expected_old_sha=draft.commit_sha,
                    )
                return _render_orphaned(
                    request,
                    task_id=task_id,
                    trial_id=trial_id,
                    commit_sha=draft.commit_sha,
                    branch=branch,
                    banner=f"push error: {exc.__class__.__name__}",
                    recovery_kind="auto",
                )

    # Phase 3: submit, with retry-before-orphan + read-back.
    submission = ImplementSubmission(
        status=draft.status,
        trial_id=trial_id,
        commit_sha=draft.commit_sha if draft.status == "success" else None,
    )
    outcome, banner = _retry_submit_with_readback(
        store=store, task_id=task_id, token=token, submission=submission
    )
    if outcome == "ok":
        _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
        return _render_submitted(
            request,
            task_id=task_id,
            trial_id=trial_id,
            commit_sha=draft.commit_sha,
            branch=branch if draft.status == "success" else None,
            status=draft.status,
        )
    return _render_orphaned(
        request,
        task_id=task_id,
        trial_id=trial_id,
        commit_sha=draft.commit_sha,
        branch=branch if draft.status == "success" else None,
        banner=banner or "submit failed",
        recovery_kind=outcome,
    )


_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


def _retry_submit_with_readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: ImplementSubmission,
) -> tuple[str, str | None]:
    """Submit with retry, then reconcile via committed-state read-back.

    Returns one of:

    - ``("ok", None)`` — the submit committed (either the first
      attempt returned cleanly, or a retry, or the read-back
      confirmed a prior committed equivalent submission).
    - ``("auto", banner)`` — orphan page; auto-recovers via reclaim.
      Used for retry exhaustion where the read-back showed the
      claim is still ours, where it shows a different claim, or
      where the task has been reclaimed back to ``pending``;
      and for the ``WrongToken`` short-circuit (the prior reclaim
      already errored our trial).
    - ``("conflict", banner)`` — orphan page; a different
      submission won the race (``ConflictingResubmission`` short-
      circuit, or the read-back saw a non-equivalent committed
      payload).
    - ``("transport", banner)`` — orphan page; an
      implementation-illegal store state was observed during
      read-back (``read_submission`` returned ``None`` for a
      ``submitted`` / ``completed`` / ``failed`` task) or the
      read-back probe itself failed.

    Exception classification (per
    ``feedback_retry_readback_test_mocks.md`` rule 1):

    - ``WrongToken`` and ``ConflictingResubmission`` are definitive
      short-circuits. ``WrongToken`` only fires when
      ``task.state == "claimed"`` with a non-matching token, which
      a successful prior submit never produces (submit preserves
      our token). ``ConflictingResubmission`` is the spec-defined
      "different payload won" signal.
    - ``IllegalTransition`` is **not** a definitive short-circuit:
      it falls through to read-back. The store raises it when
      ``task.state ∉ {"claimed", "submitted"}`` at call time,
      which after a transport-indeterminate first attempt may
      mean ``pending`` (we lost), ``completed``/``failed`` (we
      won and the orchestrator already terminalized), or
      ``submitted`` by another worker (conflict). Read-back
      disambiguates.
    - Other exceptions are retried with backoff; on exhaustion,
      read-back resolves.
    """
    last_exc: BaseException | None = None
    needs_readback = False
    for delay in _RETRY_DELAYS_S:
        try:
            store.submit(task_id, token, submission)
            return "ok", None
        except WrongToken as exc:
            return "auto", _wire_error_banner(exc)
        except ConflictingResubmission as exc:
            return "conflict", _wire_error_banner(exc)
        except IllegalTransition as exc:
            last_exc = exc
            needs_readback = True
            break
        except Exception as exc:  # noqa: BLE001 — transport-shaped
            last_exc = exc
            time.sleep(delay)

    if not needs_readback and last_exc is None:
        # All retries returned cleanly is impossible (we'd return
        # "ok" inside the loop). Defensive.
        return "transport", "submit returned without exception or commit"

    return _readback(
        store=store, task_id=task_id, token=token, submission=submission, last_exc=last_exc
    )


def _readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: ImplementSubmission,
    last_exc: BaseException | None,
) -> tuple[str, str | None]:
    last_name = last_exc.__class__.__name__ if last_exc else "unknown"
    try:
        task = store.read_task(task_id)
    except Exception as exc:  # noqa: BLE001
        return (
            "transport",
            f"transport failure after retries; read-back failed: {exc.__class__.__name__}",
        )
    state = task.state
    if state == "claimed":
        if task.claim is not None and task.claim.token == token:
            return ("auto", f"transport failure after retries: {last_name}")
        return "auto", "eden://error/wrong-token"
    if state in {"submitted", "completed", "failed"}:
        try:
            prior = store.read_submission(task_id)
        except Exception as exc:  # noqa: BLE001
            return (
                "transport",
                (
                    "transport failure after retries; "
                    f"read-submission failed: {exc.__class__.__name__}"
                ),
            )
        if prior is None:
            return (
                "transport",
                "store invariant violation: submission missing for terminal/submitted task",
            )
        if submissions_equivalent(prior, submission):
            return "ok", None
        return "conflict", "eden://error/conflicting-resubmission"
    # state == "pending"
    return ("auto", f"transport failure after retries; task reclaimed: {last_name}")


def _render_draft(
    request: Request,
    *,
    session: Any,
    task_id: str,
    proposal: Proposal,
    trial_id: str,
    form_state: dict[str, str],
    errors: Any,
    status_code: int,
) -> HTMLResponse:
    artifacts_dir = request.app.state.artifacts_dir
    rationale = read_proposal_rationale(proposal, artifacts_dir)
    branch = _branch_name(proposal.slug, trial_id)
    repo_path = getattr(request.app.state.repo, "path", None)
    return request.app.state.templates.TemplateResponse(
        request,
        "implementer_claim.html",
        {
            "session": session,
            "task_id": task_id,
            "proposal": proposal,
            "rationale": rationale,
            "branch": branch,
            "repo_path": repo_path,
            "form_state": form_state,
            "errors": errors,
        },
        status_code=status_code,
    )


def _render_submitted(
    request: Request,
    *,
    task_id: str,
    trial_id: str,
    commit_sha: str | None,
    branch: str | None,
    status: str,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "implementer_submitted.html",
        {
            "task_id": task_id,
            "trial_id": trial_id,
            "commit_sha": commit_sha,
            "branch": branch,
            "status": status,
        },
    )


def _render_orphaned(
    request: Request,
    *,
    task_id: str,
    trial_id: str,
    commit_sha: str | None,
    branch: str | None,
    banner: str,
    recovery_kind: str,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "implementer_orphaned.html",
        {
            "task_id": task_id,
            "trial_id": trial_id,
            "commit_sha": commit_sha,
            "branch": branch,
            "banner": banner,
            "recovery_kind": recovery_kind,
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
    """Reject the request with 403; suppress htmx swap on error body."""
    headers: dict[str, str] = {}
    if request is not None and is_htmx_request(request):
        headers["hx-reswap"] = "none"
    return HTMLResponse(
        content="CSRF token missing or invalid",
        status_code=403,
        headers=headers,
    )


def _empty_form_state() -> dict[str, str]:
    return {"status": "success", "commit_sha": "", "description": ""}


def _iso(dt: Any) -> str:
    """Emit Zulu-suffixed ISO 8601 (mirrors planner.py)."""
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[: -len("+00:00")] + "Z"
    return s


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
