"""Executor-module routes.

Implements the spec-to-code map pinned in §C of the Phase 9c plan.
The flow is: list pending execution tasks → claim with TTL +
server-owned variant_id → render draft form (read-only idea
context, optional inline content) → submit, which runs

1. validate the form (slug pattern, hex commit_sha when status=success)
2. (status=success only) check the §3.3 reachability invariants
   against the bare repo: commit_exists + is_ancestor(parent, commit_sha)
3. (status=success only) Pre-Phase-1 ref-collision guard
4. Phase 1: ``store.create_variant`` (status=starting; no commit_sha)
5. Phase 2 (status=success only): ``repo.create_ref(work/<branch>, sha)``
6. Phase 3: ``store.submit`` with retry-before-orphan plus a
   committed-state read-back that keys off
   ``submissions_equivalent`` rather than worker_id

variant_id is generated server-side at claim time and stored in
``_CLAIMS``; it never appears in the request surface.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from eden_contracts import Idea, Variant
from eden_storage import (
    DispatchError,
    NoOpVariant,
    StorageError,
    VariantSubmission,
)
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..forms import parse_implement_form
from ._helpers import (
    csrf_ok,
    get_session,
    is_htmx_request,
    read_idea_content,
    read_idea_manifest,
)
from ._submit_readback import submit_with_readback, wire_error_banner

router = APIRouter(prefix="/executor")


# In-memory mapping of (csrf_token, task_id) -> (claim token, variant_id).
# Keyed by session.csrf so two browser sessions on the same configured
# worker_id cannot hijack each other's claims, and so the variant_id
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


def _new_variant_id() -> str:
    return f"variant-{uuid.uuid4().hex[:12]}"


def _branch_name(slug: str, variant_id: str) -> str:
    # Field order matches the integrator's variant-branch shape
    # (`variant/<variant_id>-<slug>`, spec ch06 §3.2) so operators
    # reading Forgejo see consistent `<variant_id>-<slug>` ordering
    # across both refs.
    return f"work/{variant_id}-{slug}"


def _list_recent_variants(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_variants()
    return items[-limit:]


def _check_success_draft_gates(
    repo: Any,
    *,
    commit_sha: str,
    parents: tuple[str, ...] | list[str],
    branch: str,
) -> str | None:
    """Run the §3.3 / §C pre-submit gates for a ``status="success"`` draft.

    Returns the first failure's user-facing error string, or ``None``
    when every gate passes. The caller wraps the message in
    ``errors.add(0, "commit_sha", ...)`` and renders the draft form.

    Gates run in spec order (M-2 refactor; see
    [`docs/audits/2026-05-20-code-quality-audit.md`][audit] §3.3):

    1. Commit exists locally (push-then-fetch confirmation).
    2. Commit descends from every declared parent.
    3. Commit tree is resolvable (fail-closed on transient git failures).
    4. Commit tree is non-identical to every parent tree (§3.3 non-no-op
       invariant; the Web UI does the full tree check since the host has
       full git access).
    5. ``refs/heads/<branch>`` does not yet exist (Phase 1 collision guard).

    [audit]: ../../../../../../docs/audits/2026-05-20-code-quality-audit.md
    """
    if not repo.commit_exists(commit_sha):
        return (
            f"commit {commit_sha!r} not found in the bare repo; did you push it?"
        )
    for parent in parents:
        if not repo.is_ancestor(parent, commit_sha):
            return (
                f"commit {commit_sha!r} does not descend from declared parent {parent!r}"
            )
    try:
        variant_tree = repo.commit_tree_sha(commit_sha)
        parent_trees = [repo.commit_tree_sha(p) for p in parents]
    except Exception as exc:  # noqa: BLE001 — git-shaped
        # Fail-closed: a transient git read failure must not allow a
        # no-op variant past this gate, since the task-store-server's
        # SHA-equality fast path won't catch the empty-commit-on-parent
        # case in the default deployment.
        return (
            f"failed to resolve commit tree for the §3.3 non-no-op "
            f"check: {exc.__class__.__name__}. Refusing submit; "
            "retry once the local git state is healthy."
        )
    if all(t == variant_tree for t in parent_trees):
        return (
            f"commit {commit_sha!r} has the same git tree as every parent; "
            "refusing to submit a no-op variant (spec/v0/03-roles.md §3.3)"
        )
    if repo.ref_exists(f"refs/heads/{branch}"):
        return (
            f"branch refs/heads/{branch} already exists; "
            "reclaim or pick a different idea slug"
        )
    return None


def _build_starting_variant(
    *,
    variant_id: str,
    experiment_id: str,
    idea_id: str,
    parent_commits: tuple[str, ...] | list[str],
    branch: str,
    started_at: str,
    description: str | None,
) -> Variant:
    """Construct the Phase-1 ``Variant(status="starting")`` row."""
    kwargs: dict[str, Any] = {
        "variant_id": variant_id,
        "experiment_id": experiment_id,
        "idea_id": idea_id,
        "status": "starting",
        "parent_commits": list(parent_commits),
        "branch": branch,
        "started_at": started_at,
    }
    if description is not None:
        kwargs["description"] = description
    return Variant(**kwargs)


def _phase1_create_starting_variant(
    *,
    store: Any,
    request: Request,
    task_id: str,
    variant_id: str,
    idea: Idea,
    draft: Any,
    branch: str,
    started_at: str,
) -> HTMLResponse | None:
    """Phase 1: ``store.create_variant(status="starting")``.

    Returns ``None`` on success. On ``DispatchError`` renders the
    wire-error banner; on any other exception (transport-shaped)
    renders the orphan page — the variant may or may not have
    committed on the server, and the TTL→reclaim→variant→error
    recovery is the same either way.
    """
    variant = _build_starting_variant(
        variant_id=variant_id,
        experiment_id=request.app.state.experiment_id,
        idea_id=idea.idea_id,
        parent_commits=idea.parent_commits,
        branch=branch,
        started_at=started_at,
        description=draft.description,
    )
    try:
        store.create_variant(variant)
    except DispatchError as exc:
        return _render_error(
            request,
            f"create_variant failed for {variant_id}: {wire_error_banner(exc)}",
        )
    except Exception as exc:  # noqa: BLE001 — transport-shaped
        return _render_orphaned(
            request,
            task_id=task_id,
            variant_id=variant_id,
            commit_sha=draft.commit_sha,
            branch=branch if draft.status == "success" else None,
            banner=f"create_variant transport failure: {exc.__class__.__name__}",
            recovery_kind="transport",
        )
    return None


def _phase3_submit_with_readback(
    *,
    request: Request,
    store: Any,
    repo: Any,
    session: Any,
    task_id: str,
    token: str,
    variant_id: str,
    draft: Any,
    branch: str,
) -> HTMLResponse | RedirectResponse:
    """Phase 3: ``store.submit`` with retry-before-orphan + read-back.

    On ``NoOpVariant`` from the server-side §3.3 fast path, delegate to
    :func:`_handle_noop_variant_fallback` which runs the chapter-06 §3.4
    compensating-delete ladder and resubmits as ``status="error"``.
    """
    submission = VariantSubmission(
        status=draft.status,
        variant_id=variant_id,
        commit_sha=draft.commit_sha if draft.status == "success" else None,
        artifacts_uri=draft.artifacts_uri,
    )
    try:
        outcome, banner = submit_with_readback(
            store=store, task_id=task_id, token=token, submission=submission
        )
    except NoOpVariant:
        return _handle_noop_variant_fallback(
            request=request,
            store=store,
            repo=repo,
            session=session,
            task_id=task_id,
            token=token,
            variant_id=variant_id,
            draft=draft,
            branch=branch,
        )
    if outcome == "ok":
        _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
        return _render_submitted(
            request,
            task_id=task_id,
            variant_id=variant_id,
            commit_sha=draft.commit_sha,
            branch=branch if draft.status == "success" else None,
            status=draft.status,
        )
    return _render_orphaned(
        request,
        task_id=task_id,
        variant_id=variant_id,
        commit_sha=draft.commit_sha,
        branch=branch if draft.status == "success" else None,
        banner=banner or "submit failed",
        recovery_kind=outcome,
    )


def _handle_noop_variant_fallback(
    *,
    request: Request,
    store: Any,
    repo: Any,
    session: Any,
    task_id: str,
    token: str,
    variant_id: str,
    draft: Any,
    branch: str,
) -> HTMLResponse | RedirectResponse:
    """Server-side NoOpVariant rejection: roll back refs + resubmit as error.

    The local pre-submit ``_check_success_draft_gates`` missed (e.g. a
    transient git read failure in ``commit_tree_sha``). The variant is
    now in ``starting`` and the ``work/*`` ref has been created locally
    and possibly pushed to origin. Delete remote-first per chapter-06
    §3.4 compensating-delete order, then resubmit as ``status="error"``
    so the variant terminalizes cleanly. Mirrors the executor host's
    ``NoOpVariant`` fallback in ``subprocess_mode``.
    """
    if draft.status == "success" and _repo_has_origin(repo):
        with contextlib.suppress(Exception):
            repo.delete_remote_ref(f"refs/heads/{branch}")
    if draft.status == "success":
        with contextlib.suppress(Exception):
            repo.delete_ref(
                f"refs/heads/{branch}", expected_old_sha=draft.commit_sha
            )
    error_submission = VariantSubmission(
        status="error", variant_id=variant_id, commit_sha=None,
    )
    outcome, banner = submit_with_readback(
        store=store,
        task_id=task_id,
        token=token,
        submission=error_submission,
    )
    if outcome == "ok":
        _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
        return _render_submitted(
            request,
            task_id=task_id,
            variant_id=variant_id,
            commit_sha=None,
            branch=None,
            status="error",
        )
    return _render_orphaned(
        request,
        task_id=task_id,
        variant_id=variant_id,
        commit_sha=draft.commit_sha,
        branch=branch if draft.status == "success" else None,
        banner=(
            "server rejected as no-op; "
            f"follow-up error-submit also failed: {banner or outcome}"
        ),
        recovery_kind=outcome,
    )


def _phase2_write_work_ref(
    repo: Any, *, branch: str, commit_sha: str
) -> str | None:
    """Create the local ``work/<branch>`` ref and (if origin set) push it.

    On any failure: roll back the local ref so we don't leave a local-
    only ``work/*`` the orchestrator can never integrate, and return a
    user-facing banner string. Returns ``None`` on success.

    The next host startup's ``fetch_all_heads --prune`` is the backstop
    if the rollback ``delete_ref`` itself fails.
    """
    try:
        repo.create_ref(f"refs/heads/{branch}", commit_sha)
    except Exception as exc:  # noqa: BLE001 — git/transport-shaped
        return f"repo error: {exc.__class__.__name__}"
    if _repo_has_origin(repo):
        try:
            repo.push_ref(f"refs/heads/{branch}")
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                repo.delete_ref(
                    f"refs/heads/{branch}", expected_old_sha=commit_sha
                )
            return f"push error: {exc.__class__.__name__}"
    return None


@router.get("/", response_class=HTMLResponse, response_model=None)
async def list_pending(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    try:
        pending = store.list_tasks(kind="execution", state="pending")
        recent = _list_recent_variants(store)
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request, f"task-store transport failure: {exc.__class__.__name__}"
        )
    config = request.app.state.experiment_config
    artifacts_dir = request.app.state.artifacts_dir
    pending_rows, read_failed_count = _build_executor_pending_rows(
        store, pending, artifacts_dir
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "executor_list.html",
        {
            "session": session,
            "pending": pending,
            "pending_rows": pending_rows,
            "read_failed_count": read_failed_count,
            "objective": config.objective,
            "recent_variants": recent,
            "banner": request.query_params.get("banner"),
        },
    )


def _build_executor_pending_rows(
    store: Any, pending: list[Any], artifacts_dir: Any
) -> tuple[list[dict[str, Any]], int]:
    """Per pending execution task, build a preview row with idea context.

    Plan §D.4 — one ``read_idea`` per row. ``StorageNotFound`` →
    ``idea=None`` and the template renders "(idea unavailable)".
    Transport-shaped → ``read_failed=True`` and the page-level
    counter increments.
    """
    from eden_storage.errors import NotFound as StorageNotFound

    rows: list[dict[str, Any]] = []
    read_failed = 0
    for task in pending:
        idea: Any | None = None
        idea_content: str | None = None
        read_failed_row = False
        try:
            idea = store.read_idea(task.payload.idea_id)
        except StorageNotFound:
            idea = None
        except Exception:  # noqa: BLE001 — transport-shaped
            idea = None
            read_failed_row = True
            read_failed += 1
        if idea is not None:
            try:
                idea_content = read_idea_content(idea, artifacts_dir)
            except Exception:  # noqa: BLE001 — defensive
                idea_content = None
        rows.append(
            {
                "task": task,
                "idea": idea,
                "idea_content": idea_content,
                "target": task.target,
                "lineage_link": f"/admin/tasks/{task.task_id}/",
                "read_failed": read_failed_row,
            }
        )
    return rows, read_failed


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
        return RedirectResponse(
            url=f"/executor/?banner={banner}", status_code=303
        )
    variant_id = _new_variant_id()
    _CLAIMS[_claim_key(session.csrf, task_id)] = (result.worker_id, variant_id)
    return RedirectResponse(url=f"/executor/{task_id}/draft", status_code=303)


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
            url="/executor/?banner=claim+missing+from+session",
            status_code=303,
        )
    _, variant_id = entry
    store = request.app.state.store
    try:
        task = store.read_task(task_id)
        idea: Idea = store.read_idea(task.payload.idea_id)
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    return _render_draft(
        request,
        session=session,
        task_id=task_id,
        idea=idea,
        variant_id=variant_id,
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
            url="/executor/?banner=claim+missing+from+session",
            status_code=303,
        )
    token, variant_id = entry

    store = request.app.state.store
    repo = request.app.state.repo
    try:
        task = store.read_task(task_id)
        idea = store.read_idea(task.payload.idea_id)
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))

    draft, errors, form_state = _parse_submit_form(form)
    if draft is None:
        return _render_draft(
            request,
            session=session,
            task_id=task_id,
            idea=idea,
            variant_id=variant_id,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )

    branch = _branch_name(idea.slug, variant_id)

    if draft.status == "success":
        gate_response = _run_success_draft_gates(
            request,
            session=session,
            repo=repo,
            task_id=task_id,
            idea=idea,
            variant_id=variant_id,
            draft=draft,
            branch=branch,
            errors=errors,
            form_state=form_state,
        )
        if gate_response is not None:
            return gate_response

    return _drive_submit_phases(
        request=request,
        store=store,
        repo=repo,
        session=session,
        task_id=task_id,
        token=token,
        variant_id=variant_id,
        idea=idea,
        draft=draft,
        branch=branch,
    )


def _drive_submit_phases(
    *,
    request: Request,
    store: Any,
    repo: Any,
    session: Any,
    task_id: str,
    token: str,
    variant_id: str,
    idea: Idea,
    draft: Any,
    branch: str,
) -> HTMLResponse | RedirectResponse:
    """Run Phase 1 (create_variant) + Phase 2 (write/push work ref) +
    Phase 3 (submit + read-back) and return the final response."""
    now: Callable[[], Any] = request.app.state.now
    started_at = _iso(now())

    phase1_error = _phase1_create_starting_variant(
        store=store,
        request=request,
        task_id=task_id,
        variant_id=variant_id,
        idea=idea,
        draft=draft,
        branch=branch,
        started_at=started_at,
    )
    if phase1_error is not None:
        return phase1_error

    if draft.status == "success":
        phase2_response = _phase2_publish_work_ref_or_orphan(
            request,
            repo=repo,
            task_id=task_id,
            variant_id=variant_id,
            draft=draft,
            branch=branch,
        )
        if phase2_response is not None:
            return phase2_response

    return _phase3_submit_with_readback(
        request=request,
        store=store,
        repo=repo,
        session=session,
        task_id=task_id,
        token=token,
        variant_id=variant_id,
        draft=draft,
        branch=branch,
    )


def _phase2_publish_work_ref_or_orphan(
    request: Request,
    *,
    repo: Any,
    task_id: str,
    variant_id: str,
    draft: Any,
    branch: str,
) -> HTMLResponse | None:
    """Phase 2: create the local ``work/*`` ref and push to origin if configured.

    Returns the orphan-render response on git/transport failure, or
    ``None`` when the ref-write + push (if any) succeeded.
    Phase 10d follow-up B §D.7.
    """
    assert draft.commit_sha is not None
    ref_error = _phase2_write_work_ref(
        repo, branch=branch, commit_sha=draft.commit_sha
    )
    if ref_error is None:
        return None
    return _render_orphaned(
        request,
        task_id=task_id,
        variant_id=variant_id,
        commit_sha=draft.commit_sha,
        branch=branch,
        banner=ref_error,
        recovery_kind="auto",
    )


def _parse_submit_form(form: Any) -> tuple[Any, Any, dict[str, str]]:
    """Parse the implement-form fields. Returns ``(draft, errors, form_state)``."""
    status_raw = str(form.get("status") or "")
    commit_sha_raw = str(form.get("commit_sha") or "")
    description_raw = str(form.get("description") or "")
    artifacts_uri_raw = str(form.get("artifacts_uri") or "")
    draft, errors = parse_implement_form(
        status_raw=status_raw,
        commit_sha_raw=commit_sha_raw,
        description_raw=description_raw,
        artifacts_uri_raw=artifacts_uri_raw,
    )
    form_state = {
        "status": status_raw or "success",
        "commit_sha": commit_sha_raw,
        "description": description_raw,
        "artifacts_uri": artifacts_uri_raw,
    }
    return draft, errors, form_state


def _run_success_draft_gates(
    request: Request,
    *,
    session: Any,
    repo: Any,
    task_id: str,
    idea: Idea,
    variant_id: str,
    draft: Any,
    branch: str,
    errors: Any,
    form_state: dict[str, str],
) -> HTMLResponse | None:
    """§C reachability + §3.3 non-no-op + Phase-1 ref-collision gates.

    Fetch from origin first so a freshly-pushed executor commit is
    visible — fetch failure doesn't block submit; the commit-exists
    gate surfaces a clear error if the commit really isn't local.

    Returns the draft re-render response on gate failure, or ``None``
    when all gates pass.
    """
    assert draft.commit_sha is not None
    if _repo_has_origin(repo):
        with contextlib.suppress(Exception):
            repo.fetch_all_heads()
    gate_error = _check_success_draft_gates(
        repo,
        commit_sha=draft.commit_sha,
        parents=idea.parent_commits,
        branch=branch,
    )
    if gate_error is None:
        return None
    errors.add(0, "commit_sha", gate_error)
    return _render_draft(
        request,
        session=session,
        task_id=task_id,
        idea=idea,
        variant_id=variant_id,
        form_state=form_state,
        errors=errors,
        status_code=400,
    )


def _render_draft(
    request: Request,
    *,
    session: Any,
    task_id: str,
    idea: Idea,
    variant_id: str,
    form_state: dict[str, str],
    errors: Any,
    status_code: int,
) -> HTMLResponse:
    artifacts_dir = request.app.state.artifacts_dir
    content = read_idea_content(idea, artifacts_dir)
    idea_manifest = read_idea_manifest(idea, artifacts_dir)
    branch = _branch_name(idea.slug, variant_id)
    repo_path = getattr(request.app.state.repo, "path", None)
    clone_url = getattr(request.app.state, "clone_url", None)
    return request.app.state.templates.TemplateResponse(
        request,
        "executor_claim.html",
        {
            "session": session,
            "task_id": task_id,
            "idea": idea,
            "content": content,
            "idea_manifest": idea_manifest,
            "branch": branch,
            "repo_path": repo_path,
            "clone_url": clone_url,
            "form_state": form_state,
            "errors": errors,
        },
        status_code=status_code,
    )


def _render_submitted(
    request: Request,
    *,
    task_id: str,
    variant_id: str,
    commit_sha: str | None,
    branch: str | None,
    status: str,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "executor_submitted.html",
        {
            "task_id": task_id,
            "variant_id": variant_id,
            "commit_sha": commit_sha,
            "branch": branch,
            "status": status,
        },
    )


def _render_orphaned(
    request: Request,
    *,
    task_id: str,
    variant_id: str,
    commit_sha: str | None,
    branch: str | None,
    banner: str,
    recovery_kind: str,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "executor_orphaned.html",
        {
            "task_id": task_id,
            "variant_id": variant_id,
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
    return {
        "status": "success",
        "commit_sha": "",
        "description": "",
        "artifacts_uri": "",
    }


def _iso(dt: Any) -> str:
    """Emit Zulu-suffixed ISO 8601 (mirrors ideator.py)."""
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[: -len("+00:00")] + "Z"
    return s


