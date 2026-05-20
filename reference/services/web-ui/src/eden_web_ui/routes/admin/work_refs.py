"""Work-ref GC routes: ``/admin/work-refs/`` list + delete."""

from __future__ import annotations

from typing import Any

from eden_contracts import Variant
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .._helpers import csrf_ok, get_session
from ._common import (
    _REF_DELETE_OUTCOMES,
    _WORK_REF_RE,
    _csrf_failure_response,
    _outcome,
    _read_failure_response,
    _repo_has_origin,
    _variant_terminal_handled,
)

router = APIRouter(prefix="/admin")


@router.get("/work-refs/", response_class=HTMLResponse, response_model=None)
async def work_refs_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    repo = request.app.state.repo
    outcome = _outcome(request, "deleted", "error", _REF_DELETE_OUTCOMES)
    if repo is None:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin_work_refs.html",
            {
                "session": session,
                "csrf_token": session.csrf,
                "repo_enabled": False,
                "groups": None,
                "outcome": outcome,
            },
        )
    store = request.app.state.store
    if _repo_has_origin(repo):
        try:
            repo.fetch_all_heads()
        except Exception:  # noqa: BLE001 — git or transport
            return _read_failure_response(
                request, "could not fetch from forgejo"
            )
    try:
        groups = _classify_work_refs(repo, store)
    except Exception:  # noqa: BLE001 — git or transport
        return _read_failure_response(request, "could not list work refs")
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_work_refs.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "repo_enabled": True,
            "groups": groups,
            "outcome": outcome,
        },
    )


class _DeleteBail(Exception):
    """Raised by the work-refs delete helpers to short-circuit with a banner."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _find_delete_target(
    groups: dict[str, list[dict[str, Any]]], ref_name: str
) -> dict[str, Any]:
    """Return the work-ref entry matching ``ref_name`` from eligible/orphan groups.

    Raises :class:`_DeleteBail` with ``not-eligible`` if the ref is
    listed but not deletable, or ``not-found`` if it isn't listed at
    all (it was deleted out from under the page between GET and POST).
    """
    for entry in (*groups["eligible"], *groups["orphan"]):
        if entry["ref_name"] == ref_name:
            return entry
    for entry in groups["not_eligible"]:
        if entry["ref_name"] == ref_name:
            raise _DeleteBail("not-eligible")
    raise _DeleteBail("not-found")


def _classify_delete_git_error(exc: Any, *, remote: bool) -> str:
    """Map a ``git update-ref -d`` / ``git push --delete`` failure to a banner key.

    ``git`` exits 1 with operationally-distinct stderr strings for
    "ref vanished between read and delete" vs "CAS mismatch". The
    remote-delete uses ``--force-with-lease`` semantics so its strings
    differ from the local-delete's update-ref message; we keep the two
    classifiers side-by-side here.
    """
    stderr = (getattr(exc, "stderr", "") or "").lower()
    if remote:
        if "stale info" in stderr or "rejected" in stderr:
            return "ref-changed"
        if "deleting unknown ref" in stderr or "remote ref does not exist" in stderr:
            return "not-found"
    else:
        if "expected" in stderr and "but is" in stderr:
            return "ref-changed"
        if "unable to resolve reference" in stderr:
            return "not-found"
    # Caller re-raises on empty classification (unknown failure mode).
    return ""


@router.post("/work-refs/delete", response_model=None)
async def work_refs_delete(
    request: Request,
    ref_name: str = Form(""),
    expected_old_sha: str = Form(""),  # noqa: ARG001 — accepted for symmetry, never trusted
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    repo = request.app.state.repo
    if repo is None:
        return RedirectResponse(
            url="/admin/work-refs/?error=invalid-ref-name", status_code=303
        )
    if not _WORK_REF_RE.match(ref_name) or ".." in ref_name:
        return RedirectResponse(
            url="/admin/work-refs/?error=invalid-ref-name", status_code=303
        )
    store = request.app.state.store
    groups = _classify_work_refs(repo, store)
    try:
        target = _find_delete_target(groups, ref_name)
    except _DeleteBail as bail:
        return RedirectResponse(
            url=f"/admin/work-refs/?error={bail.error}", status_code=303
        )
    live_sha = target["current_sha"]
    from eden_git.repo import GitError

    # Phase 10d follow-up B §D.7c: when origin is configured, the
    # remote IS the source of truth — delete there first.
    if _repo_has_origin(repo):
        try:
            repo.delete_remote_ref(ref_name, expected_sha=live_sha)
        except GitError as exc:
            banner = _classify_delete_git_error(exc, remote=True)
            if not banner:
                raise
            return RedirectResponse(
                url=f"/admin/work-refs/?error={banner}", status_code=303
            )

    try:
        repo.delete_ref(ref_name, expected_old_sha=live_sha)
    except GitError as exc:
        banner = _classify_delete_git_error(exc, remote=False)
        if not banner:
            raise
        return RedirectResponse(
            url=f"/admin/work-refs/?error={banner}", status_code=303
        )
    return RedirectResponse(
        url="/admin/work-refs/?deleted=ok", status_code=303
    )


def _classify_work_refs(repo: Any, store: Any) -> dict[str, list[dict[str, Any]]]:
    """Group ``refs/heads/work/*`` refs by GC eligibility.

    Ownership is keyed off exact ``variant.branch`` equality (chunk 9e
    plan §A.7), not by parsing the ref name.
    """
    variants = store.list_variants()
    branch_index: dict[str, Variant] = {
        tr.branch: tr for tr in variants if tr.branch is not None
    }
    pairs = repo.list_refs("refs/heads/work/*")
    eligible: list[dict[str, Any]] = []
    not_eligible: list[dict[str, Any]] = []
    orphan: list[dict[str, Any]] = []
    for refname, current_sha in pairs:
        branch_name = refname.removeprefix("refs/heads/")
        variant = branch_index.get(branch_name)
        entry: dict[str, Any] = {
            "ref_name": refname,
            "current_sha": current_sha,
            "branch_name": branch_name,
            "variant": variant,
        }
        if variant is None:
            entry["reason"] = "no variant owns this ref"
            orphan.append(entry)
            continue
        if not _variant_terminal_handled(variant):
            entry["reason"] = (
                f"variant is {variant.status}"
                + (
                    " (integrator has not yet integrated)"
                    if variant.status == "success" and variant.variant_commit_sha is None
                    else ""
                )
            )
            not_eligible.append(entry)
            continue
        if variant.commit_sha != current_sha:
            entry["reason"] = (
                "ref SHA does not match variant.commit_sha (manual rewrite?)"
            )
            not_eligible.append(entry)
            continue
        entry["reason"] = (
            f"variant {variant.variant_id} is {variant.status}; safe to delete"
        )
        eligible.append(entry)
    return {
        "eligible": eligible,
        "not_eligible": not_eligible,
        "orphan": orphan,
    }
