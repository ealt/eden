"""Ideator-module routes.

Implements the spec-to-code map pinned in section §C of the Phase
9 plan: list pending ideation tasks, claim with a TTL, draft ideas,
submit. Errors propagate as canonical wire-error names so the user
sees an honest banner.

The Phase-3 retry policy on ``submit`` leverages chapter 07 §2.4 /
§8.1: a content-equivalent resubmit returns 200, so transport
failures are retried up to 3 times with exponential backoff before
the orphaned-ideas error page is reached.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from eden_contracts import Idea
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    IdeaSubmission,
    IllegalTransition,
    InvalidPrecondition,
    NotClaimed,
    StorageError,
    WrongClaimant,
)
from eden_storage.submissions import submissions_equivalent
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from ..artifacts import (
    UploadedFile,
    predict_artifact_uri,
    write_artifact_bundle,
)
from ..forms import FormErrors, IdeaDraft, format_validation_errors, parse_idea_rows
from ._helpers import csrf_ok, get_session, htmx_aware_redirect, is_htmx_request
from ._submit_readback import wire_error_banner

router = APIRouter(prefix="/ideator")


# In-memory mapping of (csrf_token, task_id) -> claim token. The CSRF
# token is per-session (rotates on session-secret rotation), so two
# browser sessions with the same configured worker_id cannot reuse
# each other's claims. We deliberately do not persist this across
# restarts — a UI restart drops claim tokens and the TTL sweep
# recovers stranded tasks (eden_dispatch.sweep_expired_claims).
_CLAIMS: dict[tuple[str, str], str] = {}

# In-memory mapping of (csrf_token, task_id) -> draft form rows. Holds
# the most-recently-typed idea rows so a navigation away from the
# claim/submit page (back button, refresh, nav click) doesn't lose the
# user's input. Same key shape as _CLAIMS so the buffer naturally dies
# with the session; cleared on successful submit / status=error
# submit. Written on every POST that carries idea rows
# (add_row + submit, including validation-error re-render).
_DRAFT_BUFFERS: dict[tuple[str, str], list[dict[str, str]]] = {}


def _claim_key(session_csrf: str, task_id: str) -> tuple[str, str]:
    return (session_csrf, task_id)


def _list_recent_ideas(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_ideas()
    return items[-limit:]


def _list_recent_variants(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_variants()
    return items[-limit:]


def _list_recent_integrated_variants(
    store: Any, *, limit: int = 10
) -> list[Any]:
    """Return the most recent variants with a ``variant_commit_sha`` set.

    Surfaces SHAs the operator can paste into the ideator form's
    ``parent_commits`` field. Filters to ``status == "success"`` AND
    ``variant_commit_sha is not None`` — i.e. variants the integrator
    has produced a canonical squash commit for, per chapter 6 §3.4.
    """
    items = store.list_variants(status="success")
    integrated = [v for v in items if v.variant_commit_sha is not None]
    return integrated[-limit:]


def _hint_context(request: Any, store: Any) -> dict[str, Any]:
    """Shared parent_commits-hint context for ideator pages.

    Returns ``{"base_commit_sha", "integrated_variants"}`` — the
    seed/base SHA from the deployment env (CLI ``--base-commit-sha``)
    and the most recent integrated variant SHAs. Both are intended
    as click-to-copy hints for the ``parent_commits`` field. Either
    may be empty/None in degenerate deployments.
    """
    return {
        "base_commit_sha": getattr(request.app.state, "base_commit_sha", None),
        "integrated_variants": _list_recent_integrated_variants(store),
    }


@router.get("/", response_class=HTMLResponse, response_model=None)
async def list_pending(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    pending = store.list_tasks(kind="ideation", state="pending")
    config = request.app.state.experiment_config
    ctx: dict[str, Any] = {
        "session": session,
        "pending": pending,
        "objective": config.objective,
        "evaluation_schema": config.evaluation_schema.root,
        "recent_ideas": _list_recent_ideas(store),
        "recent_variants": _list_recent_variants(store),
        "banner": request.query_params.get("banner"),
    }
    ctx.update(_hint_context(request, store))
    return request.app.state.templates.TemplateResponse(
        request,
        "ideator_list.html",
        ctx,
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
    except StorageError as exc:
        banner = wire_error_banner(exc)
        return RedirectResponse(url=f"/ideator/?banner={banner}", status_code=303)
    _CLAIMS[_claim_key(session.csrf, task_id)] = result.worker_id
    return RedirectResponse(url=f"/ideator/{task_id}/draft", status_code=303)


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
            url="/ideator/?banner=claim+missing+from+session",
            status_code=303,
        )
    config = request.app.state.experiment_config
    buffered = _DRAFT_BUFFERS.get(_claim_key(session.csrf, task_id))
    form_state = buffered if buffered else [_empty_row()]
    store = request.app.state.store
    ctx: dict[str, Any] = {
        "session": session,
        "task_id": task_id,
        "objective": config.objective,
        "evaluation_schema": config.evaluation_schema.root,
        "errors": None,
        "form_state": form_state,
        "row_indices": list(range(len(form_state))),
    }
    ctx.update(_hint_context(request, store))
    return request.app.state.templates.TemplateResponse(
        request,
        "ideator_claim.html",
        ctx,
    )


@router.post("/{task_id}/add_row", response_model=None)
async def add_row(task_id: str, request: Request):
    """Append one empty idea row to the draft form.

    Two transports, same end state:

    - With JS, htmx posts here with ``HX-Request: true``. We respond
      with the rendered ``_idea_row.html`` fragment for the new
      row only; htmx swaps it as ``beforeend`` of ``#idea-rows``
      so the user's existing input is untouched. Redirect/error
      branches send ``HX-Redirect`` on a 204 so htmx does a full
      client-side navigation rather than swapping a redirected page
      into the rows container.
    - Without JS, the same button submits the form normally and we
      re-render the whole ``ideator_claim.html`` with the
      collected state plus one more empty row, or 303 to the
      sign-in / ideator list on auth/claim failures.
    """
    session = get_session(request)
    if session is None:
        return htmx_aware_redirect(request, "/signin")
    form = await request.form()
    if not csrf_ok(session, form.get("csrf_token")):  # type: ignore[arg-type]
        return _csrf_failure_response(request)
    if _CLAIMS.get(_claim_key(session.csrf, task_id)) is None:
        return htmx_aware_redirect(
            request, "/ideator/?banner=claim+missing+from+session"
        )

    slugs = [str(v) for v in form.getlist("slug")]
    priorities = [str(v) for v in form.getlist("priority")]
    parents = [str(v) for v in form.getlist("parent_commits")]
    contents = [str(v) for v in form.getlist("content")]
    intended_kinds = [str(v) for v in form.getlist("intended_executor_kind")]
    intended_ids = [str(v) for v in form.getlist("intended_executor_id")]
    typed_state = _form_state_from_inputs(
        slugs,
        priorities,
        parents,
        contents,
        intended_executor_kinds=intended_kinds,
        intended_executor_ids=intended_ids,
    )
    # Persist typed input so a navigation away + return to GET /draft
    # re-hydrates the form. Append the new empty row so the buffered
    # row count matches what was just rendered.
    state = typed_state + [_empty_row()]
    _DRAFT_BUFFERS[_claim_key(session.csrf, task_id)] = state

    if is_htmx_request(request):
        # The htmx-enhanced path: figure out where the new row goes
        # by counting existing rows in the form, then render only
        # the partial. The user's existing rows are already on the
        # page; htmx appends ours to ``#idea-rows``.
        existing = max(
            len(form.getlist("slug")),
            len(form.getlist("priority")),
            len(form.getlist("parent_commits")),
            len(form.getlist("content")),
        )
        return request.app.state.templates.TemplateResponse(
            request,
            "_idea_row.html",
            {"i": existing, "row_state": _empty_row(), "row_errs": {}},
        )

    config = request.app.state.experiment_config
    store = request.app.state.store
    ctx: dict[str, Any] = {
        "session": session,
        "task_id": task_id,
        "objective": config.objective,
        "evaluation_schema": config.evaluation_schema.root,
        "errors": None,
        "form_state": state,
        "row_indices": list(range(len(state))),
    }
    ctx.update(_hint_context(request, store))
    return request.app.state.templates.TemplateResponse(
        request,
        "ideator_claim.html",
        ctx,
    )


def _submit_idea_error_status(
    *,
    store: Any,
    request: Request,
    session: Any,
    task_id: str,
    token: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the ``status="error"`` ideation outcome.

    The operator clicked the explicit "give up" button rather than
    drafting any ideas. ``IdeaSubmission(status="error")`` carries no
    ``idea_ids`` per spec §3.2 / §4.4.
    """
    try:
        store.submit(task_id, token, IdeaSubmission(status="error"))
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
    _DRAFT_BUFFERS.pop(_claim_key(session.csrf, task_id), None)
    return _render_submitted(request, task_id, status="error", idea_ids=())


def _render_idea_form_errors(
    *,
    request: Request,
    session: Any,
    store: Any,
    config: Any,
    task_id: str,
    errors: Any,
    form_state: list[dict[str, str]],
    slugs: list[str],
) -> HTMLResponse:
    """Re-render the idea draft form with form-level errors.

    Also buffers the typed state into ``_DRAFT_BUFFERS`` so a
    navigation-away + return-to-GET-/draft re-hydrates the form
    (regression guard for issue #2).
    """
    _DRAFT_BUFFERS[_claim_key(session.csrf, task_id)] = form_state
    ctx: dict[str, Any] = {
        "session": session,
        "task_id": task_id,
        "objective": config.objective,
        "evaluation_schema": config.evaluation_schema.root,
        "errors": errors,
        "form_state": form_state,
        "row_indices": list(range(max(1, len(slugs) or 1))),
    }
    ctx.update(_hint_context(request, store))
    return request.app.state.templates.TemplateResponse(
        request,
        "ideator_claim.html",
        ctx,
        status_code=400,
    )


def _persist_idea_drafts(
    *,
    store: Any,
    request: Request,
    drafts: list[Any],
    draft_rows: list[int],
    uploads_per_row: dict[int, list[UploadedFile]],
) -> tuple[list[str], list[str], HTMLResponse | None, FormErrors | None]:
    """Phase 1+2: write artifacts, create ideas as drafting, flip to ready.

    Returns ``(idea_ids, slug_warnings, error_response, validation_errors)``:

    - ``(ids, warnings, None, None)`` on success;
    - ``([], [], error_response, None)`` on a ``DispatchError`` from
      either phase (rendered as the wire-error page);
    - ``([], [], None, errors)`` on a Pydantic ``ValidationError`` raised
      by ``Idea(**kwargs)``, OR on a multi-file bundling collision /
      filename-rejection raised by :mod:`eden_web_ui.artifacts`. The
      caller re-renders the draft form with the per-row field errors —
      no store mutation has happened yet because both checks precede
      the create_idea call.

    ``slug_warnings`` carries issue-#121 soft-check messages: after each
    Idea is created, the experiment's existing ideas are scanned for
    slug collisions; matches are reported as advisory strings. Slug
    uniqueness is not a protocol invariant (idea identity is by
    ``idea_id``), so warnings never block submission.

    ``uploads_per_row`` carries issue-#120 multi-file attachments per
    draft row (empty list when only text is supplied). The artifact-
    write phase wraps text + uploads into a ``.tar.gz`` bundle when
    multiple files are present; single-file paths stay flat.

    The Phase-1 / Phase-2 split is preserved internally so a failure
    in mark-ready doesn't leave already-created drafting ideas
    dangling — recovery for those is the orchestrator's
    ttl→reclaim→idea→error path. ``draft_rows`` is the parallel
    per-draft row-index list so a ValidationError on draft N maps back
    to its original row in the multi-row form.
    """
    idea_ids: list[str] = []
    slug_warnings: list[str] = []
    artifacts_dir = request.app.state.artifacts_dir
    now: Callable[[], Any] = request.app.state.now

    for draft, row_index in zip(drafts, draft_rows, strict=True):
        idea_id = uuid.uuid4().hex
        uploads = uploads_per_row.get(row_index, [])
        # Predict the artifact URI without writing the file yet, so a
        # ValidationError on Idea construction leaves the disk clean.
        # The Idea is built first (validation barrier), THEN the
        # artifact is written, THEN the store is told about it.
        try:
            artifacts_uri = predict_artifact_uri(
                artifacts_dir,
                idea_id,
                has_text=bool(draft.content.strip()),
                uploads=uploads,
            )
        except ValueError as exc:
            errors = FormErrors()
            errors.add(row_index, "artifact", str(exc))
            return [], [], None, errors
        try:
            idea = _make_idea(
                idea_id=idea_id,
                experiment_id=request.app.state.experiment_id,
                draft=draft,
                artifacts_uri=artifacts_uri,
                now_iso=_iso(now()),
            )
        except ValidationError as exc:
            errors = format_validation_errors(exc, row=row_index)
            return [], [], None, errors
        write_artifact_bundle(
            artifacts_dir,
            idea_id,
            text_content=draft.content,
            text_filename="idea.md",
            uploads=uploads,
        )
        try:
            store.create_idea(idea)
        except DispatchError as exc:
            return [], [], _render_error(
                request,
                f"create_idea failed for {idea_id}: {wire_error_banner(exc)}",
            ), None
        idea_ids.append(idea_id)
        # Issue #121 soft-check: warn the operator when a sibling idea
        # in this experiment already uses the same slug.
        for existing in store.list_ideas():
            if existing.slug == idea.slug and existing.idea_id != idea_id:
                slug_warnings.append(
                    f"idea {idea_id}: slug {idea.slug!r} is already used "
                    f"by idea {existing.idea_id!r}"
                )

    for idea_id in idea_ids:
        try:
            store.mark_idea_ready(idea_id)
        except DispatchError as exc:
            return [], [], _render_error(
                request,
                f"mark_idea_ready failed for {idea_id}: {wire_error_banner(exc)}",
            ), None

    return idea_ids, slug_warnings, None, None


@router.post("/{task_id}/submit", response_model=None)
async def submit_idea(task_id: str, request: Request) -> HTMLResponse | RedirectResponse:
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
            url="/ideator/?banner=claim+missing+from+session",
            status_code=303,
        )

    store = request.app.state.store
    config = request.app.state.experiment_config

    if status == "error":
        return _submit_idea_error_status(
            store=store, request=request, session=session,
            task_id=task_id, token=token,
        )

    slugs = [str(v) for v in form.getlist("slug")]
    priorities = [str(v) for v in form.getlist("priority")]
    parents = [str(v) for v in form.getlist("parent_commits")]
    contents = [str(v) for v in form.getlist("content")]
    intended_kinds = [str(v) for v in form.getlist("intended_executor_kind")]
    intended_ids = [str(v) for v in form.getlist("intended_executor_id")]
    n_rows = max(len(slugs), len(priorities), len(parents), len(contents))
    uploads_per_row = await _collect_row_uploads(form, n_rows=n_rows)
    has_uploads_per_row = [bool(uploads_per_row.get(i)) for i in range(n_rows)]
    drafts, errors, draft_rows = parse_idea_rows(
        slugs,
        priorities,
        parents,
        contents,
        intended_executor_kinds=intended_kinds,
        intended_executor_ids=intended_ids,
        has_uploads_per_row=has_uploads_per_row,
    )
    if errors:
        form_state = _form_state_from_inputs(
            slugs,
            priorities,
            parents,
            contents,
            intended_executor_kinds=intended_kinds,
            intended_executor_ids=intended_ids,
        )
        return _render_idea_form_errors(
            request=request, session=session, store=store, config=config,
            task_id=task_id, errors=errors, form_state=form_state, slugs=slugs,
        )

    idea_ids, slug_warnings, persist_error, validation_errors = _persist_idea_drafts(
        store=store, request=request, drafts=drafts, draft_rows=draft_rows,
        uploads_per_row=uploads_per_row,
    )
    if validation_errors is not None:
        form_state = _form_state_from_inputs(
            slugs,
            priorities,
            parents,
            contents,
            intended_executor_kinds=intended_kinds,
            intended_executor_ids=intended_ids,
        )
        return _render_idea_form_errors(
            request=request, session=session, store=store, config=config,
            task_id=task_id, errors=validation_errors,
            form_state=form_state, slugs=slugs,
        )
    if persist_error is not None:
        return persist_error

    # Phase 3: submit, with retry-before-orphan.
    submission = IdeaSubmission(status="success", idea_ids=tuple(idea_ids))
    ok, banner = _retry_submit(store, task_id, token, submission)
    if not ok:
        return _render_orphaned(request, task_id, idea_ids, banner=banner)

    _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
    _DRAFT_BUFFERS.pop(_claim_key(session.csrf, task_id), None)
    return _render_submitted(
        request, task_id, status="success", idea_ids=tuple(idea_ids),
        slug_warnings=tuple(slug_warnings),
    )


_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


def _retry_submit(
    store: Any, task_id: str, token: str, submission: IdeaSubmission
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

    - ``NotClaimed``, ``ConflictingResubmission``, and
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
    ideas and Phase 2 has marked them ready. The work product
    that needs operator recovery is the orphaned ideas
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
        except (
            NotClaimed,
            WrongClaimant,
            ConflictingResubmission,
            InvalidPrecondition,
        ) as exc:
            return False, wire_error_banner(exc)
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
    # orphan with a state-specific banner.
    if needs_readback or last_exc is not None:
        return _readback(
            store=store,
            task_id=task_id,
            token=token,
            submission=submission,
            last_exc=last_exc,
        )

    return True, None


def _readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: IdeaSubmission,
    last_exc: BaseException | None,
) -> tuple[bool, str | None]:
    """Read-back disambiguation for the ideator's retry-exhaustion arm.

    Mirrors ``routes/executor._readback`` (and the chunk-9d
    evaluator's ``_readback``) so the ideator classifies every
    state branch the spec describes:

    - ``submitted`` / ``completed`` / ``failed`` with an equivalent
      prior submission → ``(True, None)`` (we won).
    - same states with a non-equivalent prior submission →
      ``conflicting-resubmission`` (orphan, different won).
    - same states with ``read_submission() is None`` → transport
      banner naming the store-invariant violation. This is
      implementation-illegal in the reference store but defensively
      handled.
    - ``claimed`` with our token still on the task → transport
      banner naming the underlying exception class.
    - ``claimed`` with our token gone (someone else holds the
      claim now) → ``eden://error/not-claimed``.
    - ``pending`` (sweeper / operator already reclaimed) →
      transport banner mentioning the reclaim, distinct from the
      claim-still-ours case.
    - ``read_task`` itself raises → transport banner naming the
      probe failure.
    """
    last_name = last_exc.__class__.__name__ if last_exc else "unknown"
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
        if prior is None:
            # Implementation-illegal: terminal/submitted task with
            # no recorded submission. Defensively classify as a
            # transport-flavored invariant violation rather than as
            # a conflict — the reference store would never produce
            # this state, and conflating it with conflict would
            # mis-route the operator.
            return (
                False,
                "store invariant violation: submission missing for "
                "terminal/submitted task",
            )
        if submissions_equivalent(prior, submission):
            return True, None
        return False, "eden://error/conflicting-resubmission"
    if task.state == "claimed":
        if task.claim is not None and task.claim.worker_id == token:
            return False, f"transport failure after retries: {last_name}"
        return False, "eden://error/not-claimed"
    # state == "pending"
    return (
        False,
        f"transport failure after retries; task reclaimed: {last_name}",
    )


def _make_idea(
    *,
    idea_id: str,
    experiment_id: str,
    draft: IdeaDraft,
    artifacts_uri: str,
    now_iso: str,
) -> Idea:
    kwargs: dict[str, Any] = {
        "idea_id": idea_id,
        "experiment_id": experiment_id,
        "slug": draft.slug,
        "priority": draft.priority,
        "parent_commits": list(draft.parent_commits),
        "artifacts_uri": artifacts_uri,
        "state": "drafting",
        "created_at": now_iso,
    }
    # The Idea model's `intended_executor: TaskTarget | None = None`
    # rejects an explicit-None pass-through (NotNone validator); only
    # include the kwarg when set so absent stays absent on the wire.
    if draft.intended_executor is not None:
        kwargs["intended_executor"] = draft.intended_executor
    return Idea(**kwargs)


def _iso(dt: Any) -> str:
    """Emit Zulu-suffixed ISO 8601: ``YYYY-MM-DDTHH:MM:SS(.sss)?Z``."""
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[: -len("+00:00")] + "Z"
    return s


def _empty_row() -> dict[str, str]:
    return {
        "slug": "",
        "priority": "1.0",
        "parent_commits": "",
        "content": "",
        "intended_executor_kind": "none",
        "intended_executor_id": "",
    }


def _form_state_from_inputs(
    slugs: list[str],
    priorities: list[str],
    parents: list[str],
    contents: list[str],
    intended_executor_kinds: list[str] | None = None,
    intended_executor_ids: list[str] | None = None,
) -> list[dict[str, str]]:
    kinds = intended_executor_kinds or []
    ids = intended_executor_ids or []
    n = max(len(slugs), len(priorities), len(parents), len(contents))
    out: list[dict[str, str]] = []
    for i in range(n):
        out.append(
            {
                "slug": slugs[i] if i < len(slugs) else "",
                "priority": priorities[i] if i < len(priorities) else "1.0",
                "parent_commits": parents[i] if i < len(parents) else "",
                "content": contents[i] if i < len(contents) else "",
                "intended_executor_kind": (
                    kinds[i] if i < len(kinds) else "none"
                ),
                "intended_executor_id": ids[i] if i < len(ids) else "",
            }
        )
    return out


def _render_submitted(
    request: Request,
    task_id: str,
    *,
    status: str,
    idea_ids: tuple[str, ...],
    slug_warnings: tuple[str, ...] = (),
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "ideator_submitted.html",
        {
            "task_id": task_id,
            "status": status,
            "idea_ids": idea_ids,
            "slug_warnings": slug_warnings,
        },
    )


def _render_orphaned(
    request: Request,
    task_id: str,
    idea_ids: list[str],
    *,
    banner: str | None = None,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "ideator_orphaned.html",
        {
            "task_id": task_id,
            "idea_ids": idea_ids,
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
    ``#idea-rows``). Adding the same ``HX-Trigger`` header is
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


async def _collect_row_uploads(
    form: Any, *, n_rows: int
) -> dict[int, list[UploadedFile]]:
    """Pull per-row file uploads out of the multipart form.

    Each idea row's file input is named ``files_<i>`` (issue #120 +
    ``_idea_row.html``). Browsers post one part per selected file
    under that name; ``form.getlist("files_<i>")`` returns them in
    order. Empty-filename parts (Starlette's representation of "user
    selected nothing for this row") are skipped so the bundler
    doesn't see a zero-byte phantom upload.
    """
    out: dict[int, list[UploadedFile]] = {}
    for i in range(n_rows):
        files: list[UploadedFile] = []
        for item in form.getlist(f"files_{i}"):
            if not isinstance(item, UploadFile):
                continue
            if not item.filename:
                continue
            data = await item.read()
            files.append(
                UploadedFile(
                    filename=item.filename,
                    data=data,
                    content_type=item.content_type,
                )
            )
        if files:
            out[i] = files
    return out


