"""Shared helpers for route handlers (session/CSRF lookup + cookie shape).

Issue #145 adds the per-request active-experiment resolution that lets
every per-experiment route operate against the experiment the operator
selected in the switcher (12c ``Session.selected_experiment_id``). The
load-bearing entry point is :func:`resolve_active_context` — one call at
the top of a handler that returns either a ready-to-use
:class:`ActiveContext` (resolved ``experiment_id`` + per-experiment
``store`` / ``admin_store`` / ``config``) or a ``Response`` the handler
returns verbatim (a dashboard redirect for stale / unreachable /
credential / config failures, or an "experiment not seeded" page).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

import httpx
import yaml
from eden_contracts import ExperimentConfig, Idea
from eden_service_common import load_experiment_config
from eden_storage import Store
from eden_storage.errors import NotFound
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ..artifacts import (
    is_bundle_uri,
    read_bundle_entry,
    read_bundle_manifest,
)
from ..sessions import SESSION_COOKIE_NAME, Session, SessionCodec, verify_csrf
from ..store_factory import (
    AdminTokenRejected,
    MissingAdminToken,
    TaskStoreUnreachable,
)

_DASHBOARD_PATH = "/admin/experiments/"

_CONTENT_MAX_BYTES = 1 << 20  # 1 MiB

# Bundle entry name that the viewer extracts as the inline "headline"
# when an idea-side bundle is rendered. Mirrors the convention written
# by :func:`eden_web_ui.artifacts.write_artifact_bundle` for ideator
# submissions.
IDEA_BUNDLE_HEADLINE = "content.md"
EVALUATION_BUNDLE_HEADLINE = "evaluation.md"
VARIANT_BUNDLE_HEADLINE = "variant.md"


def is_htmx_request(request: Request) -> bool:
    """True iff the request was made by htmx (carries ``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def htmx_aware_redirect(request: Request, url: str) -> Response:
    """Redirect appropriately for both htmx and no-JS clients.

    HTMX does not process 3xx responses — it follows the redirect
    transparently and swaps the redirected target's HTML into the
    configured target. For an ``add_row`` button targeted at
    ``#idea-rows`` that produces a full ``<html>`` document
    inside the rows container. The fix is to send back ``HX-Redirect``
    on a 200/204 instead; htmx intercepts that header and does a
    full client-side navigation.
    """
    if is_htmx_request(request):
        return Response(status_code=204, headers={"hx-redirect": url})
    return RedirectResponse(url=url, status_code=303)


def get_session(request: Request) -> Session | None:
    """Return the decoded session for ``request``, or ``None`` if missing/invalid."""
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    codec: SessionCodec = request.app.state.session_codec
    return codec.decode(raw)


def write_session_cookie(
    response: Response,
    *,
    encoded: str,
    secure: bool,
) -> None:
    """Set the signed session cookie on ``response`` with pinned attributes."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=encoded,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def csrf_ok(session: Session, presented: str | None) -> bool:
    """Constant-time CSRF token check, exposed for routes."""
    return verify_csrf(session, presented)


# ---------------------------------------------------------------------------
# Active-experiment resolution (issue #145)
# ---------------------------------------------------------------------------


class StaleSelection(Exception):
    """The session selects an experiment no longer in the control plane.

    Raised by :func:`resolve_active_experiment` when the operator's
    ``selected_experiment_id`` is absent from the control-plane registry
    (unregistered concurrently). Callers redirect to the dashboard with
    ``?error=stale-selection`` and clear the session field.
    """

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__(f"selected experiment {experiment_id!r} is no longer registered")


class ControlPlaneUnreachable(Exception):
    """A transport error reached the control plane during resolution."""

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__("control plane unreachable while resolving the active experiment")


class ExperimentConfigMissing(Exception):
    """No per-experiment config YAML exists at ``<config-dir>/<id>.yaml``."""

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__(f"no experiment-config YAML for {experiment_id!r}")


class ExperimentConfigInvalid(Exception):
    """The per-experiment config YAML failed to parse / validate."""

    def __init__(self, experiment_id: str, cause: BaseException) -> None:
        self.experiment_id = experiment_id
        self.cause = cause
        super().__init__(f"invalid experiment-config YAML for {experiment_id!r}: {cause}")


@dataclass(frozen=True)
class ResolvedExperiment:
    """The per-request resolved active experiment.

    ``unseeded`` is ``True`` when the control plane knows the experiment
    but the task-store-server has no experiment record yet (registered
    via the dashboard but not yet bootstrapped by ``setup-experiment`` /
    checkpoint import). Routes render an "initialize me" page instead of
    treating it as a stale selection (plan Decision 8 / Risk 11).
    """

    experiment_id: str
    unseeded: bool = False


@dataclass(frozen=True)
class ActiveContext:
    """Resolved per-request store/config bundle returned to a handler."""

    experiment_id: str
    store: Store
    admin_store: Store | None
    config: ExperimentConfig | None


def resolve_active_experiment(request: Request) -> ResolvedExperiment:
    """Resolve the experiment the current request operates against.

    Reads ``Session.selected_experiment_id`` and falls back to the
    deployment default (``app.state.experiment_id``). When no control
    plane is configured (single-experiment posture), always returns the
    deployment default with no validation call — today's behavior is
    unchanged and there is zero per-request overhead.

    For a non-default selection in control-plane mode it (1) validates
    the experiment still exists in the control plane (``StaleSelection``
    / ``ControlPlaneUnreachable`` otherwise) and (2) classifies it as
    seeded vs registered-but-unseeded against the task-store-server,
    JIT-bootstrapping the per-experiment worker credential as a side
    effect (``MissingAdminToken`` / ``AdminTokenRejected`` /
    ``TaskStoreUnreachable`` propagate to the caller).
    """
    app = request.app
    default_id: str = app.state.experiment_id
    control_plane = app.state.control_plane
    if control_plane is None:
        return ResolvedExperiment(default_id)
    session = get_session(request)
    selected = session.selected_experiment_id if session is not None else None
    if selected is None or selected == default_id:
        return ResolvedExperiment(default_id)

    try:
        control_plane.read_experiment_metadata(selected)
    except NotFound as exc:
        raise StaleSelection(selected) from exc
    except httpx.TransportError as exc:
        raise ControlPlaneUnreachable(selected) from exc

    # Classify seeded vs unseeded against the task-store-server. The
    # for_experiment call JIT-bootstraps the per-experiment worker
    # credential; a NotFound from either the bootstrap register or the
    # state read means the experiment exists in the control plane but
    # not (yet) on the task-store-server.
    factory = app.state.store_factory
    try:
        store = factory.for_experiment(selected, role="worker")
        store.read_experiment_state()
    except NotFound:
        return ResolvedExperiment(selected, unseeded=True)
    return ResolvedExperiment(selected)


def active_config(request: Request, experiment_id: str) -> ExperimentConfig:
    """Return the ``ExperimentConfig`` for ``experiment_id``.

    Single-experiment / no-control-plane mode returns the startup config
    (``app.state.experiment_config``). Control-plane mode loads
    ``<--experiment-config-dir>/<experiment_id>.yaml`` lazily (cached for
    the process lifetime — configs are immutable post-create), falling
    back to the startup config for the deployment-default experiment.
    """
    app = request.app
    default_id: str = app.state.experiment_id
    default_config: ExperimentConfig | None = app.state.experiment_config
    config_dir: Path | None = app.state.experiment_config_dir

    if app.state.control_plane is None:
        assert default_config is not None  # single-exp mode requires --experiment-config
        return default_config
    if experiment_id == default_id and default_config is not None:
        return default_config
    if config_dir is None:
        if default_config is not None:
            return default_config
        raise ExperimentConfigMissing(experiment_id)

    cache: dict[str, ExperimentConfig] = app.state.experiment_config_cache
    cached = cache.get(experiment_id)
    if cached is not None:
        return cached
    path = config_dir / f"{experiment_id}.yaml"
    try:
        config = load_experiment_config(path)
    except FileNotFoundError as exc:
        raise ExperimentConfigMissing(experiment_id) from exc
    except (ValidationError, yaml.YAMLError, ValueError) as exc:
        raise ExperimentConfigInvalid(experiment_id, exc) from exc
    cache[experiment_id] = config
    return config


def dashboard_redirect(error: str, *, exp: str | None = None) -> RedirectResponse:
    """Redirect to the cross-experiment dashboard with an ``?error=`` banner."""
    params: dict[str, str] = {"error": error}
    if exp is not None:
        params["exp"] = exp
    query = urllib.parse.urlencode(params)
    return RedirectResponse(url=f"{_DASHBOARD_PATH}?{query}", status_code=303)


def _stale_selection_redirect(request: Request) -> RedirectResponse:
    """Redirect to the dashboard AND clear the stale session selection."""
    response = dashboard_redirect("stale-selection")
    session = get_session(request)
    if session is not None:
        codec: SessionCodec = request.app.state.session_codec
        cleared = Session(
            worker_id=session.worker_id,
            csrf=session.csrf,
            selected_experiment_id=None,
        )
        write_session_cookie(
            response,
            encoded=codec.encode(cleared),
            secure=request.app.state.secure_cookies,
        )
    return response


def _unseeded_response(request: Request, experiment_id: str) -> HTMLResponse:
    """Render the "experiment registered but not seeded" page."""
    request.state.active_experiment_id = experiment_id
    return request.app.state.templates.TemplateResponse(
        request,
        "_unseeded.html",
        {"experiment_id": experiment_id},
        status_code=409,
    )


def resolve_active_context(
    request: Request, *, need_config: bool = False
) -> ActiveContext | RedirectResponse | HTMLResponse:
    """Resolve the active experiment + per-experiment store(s) for a handler.

    Returns an :class:`ActiveContext` ready to use, or a ``Response`` the
    handler returns verbatim. The ``Response`` cases are: stale-selection
    redirect (clears the session field), control-plane-unreachable /
    cannot-bootstrap-credential / task-store-unreachable / config
    redirects, and the unseeded-experiment page. On the happy path the
    resolved id is stashed on ``request.state.active_experiment_id`` so
    the template context processor renders the active experiment.
    """
    try:
        resolved = resolve_active_experiment(request)
    except StaleSelection:
        return _stale_selection_redirect(request)
    except ControlPlaneUnreachable:
        return dashboard_redirect("control-plane-unreachable")
    except (MissingAdminToken, AdminTokenRejected) as exc:
        return dashboard_redirect("cannot-bootstrap-credential", exp=exc.experiment_id)
    except TaskStoreUnreachable as exc:
        return dashboard_redirect("task-store-unreachable", exp=exc.experiment_id)

    request.state.active_experiment_id = resolved.experiment_id
    if resolved.unseeded:
        return _unseeded_response(request, resolved.experiment_id)

    factory = request.app.state.store_factory
    store = factory.for_experiment(resolved.experiment_id, role="worker")
    assert store is not None  # worker role never returns None
    admin_store = factory.for_experiment(resolved.experiment_id, role="admin")

    config: ExperimentConfig | None = None
    if need_config:
        try:
            config = active_config(request, resolved.experiment_id)
        except ExperimentConfigMissing as exc:
            return dashboard_redirect("config-missing", exp=exc.experiment_id)
        except ExperimentConfigInvalid as exc:
            return dashboard_redirect("config-invalid", exp=exc.experiment_id)

    return ActiveContext(
        experiment_id=resolved.experiment_id,
        store=store,
        admin_store=admin_store,
        config=config,
    )


def _resolve_inside_jail(
    uri: str | None, artifacts_dir: Path
) -> Path | None:
    """Trust-boundary check: resolve ``uri`` if confined to ``artifacts_dir``.

    Returns the resolved path on success; ``None`` if the URI is not
    a ``file://`` URI, points outside the jail, or doesn't resolve
    to a regular file. Used by both the inline-artifact reader and
    the bundle manifest reader so they share one path-confinement
    check.
    """
    if uri is None:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    candidate = Path(raw_path)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    base = artifacts_dir.resolve()
    if not resolved.is_relative_to(base):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _read_inline_artifact(
    uri: str | None,
    artifacts_dir: Path,
    *,
    bundle_headline: str | None = None,
) -> str | None:
    """Return the artifact text iff ``uri`` resolves inside ``artifacts_dir``.

    Trust-boundary helper used by both the idea content
    rendering (chunk 9c §A.1) and the variant-side artifact rendering
    (chunk 9d §A.1):

    - Only ``file://`` URIs are eligible. Any other scheme returns
      ``None`` so the template renders the URI as a plain link.
    - The resolved path MUST be contained within
      ``artifacts_dir.resolve()``. ``..``-traversal and absolute
      escapes are rejected via ``Path.is_relative_to``.
    - Non-file inodes (directories, sockets) return ``None``.
    - Files larger than 1 MiB return ``None``.

    Issue #120: when the resolved file is a ``.tar.gz`` bundle and
    ``bundle_headline`` names an entry inside it (e.g. ``content.md``),
    that entry's text is returned instead — so the operator's
    bundled markdown still renders inline as the headline of the
    submission. Returns ``None`` for bundles without that headline
    entry; the manifest table is rendered separately by the
    template.
    """
    resolved = _resolve_inside_jail(uri, artifacts_dir)
    if resolved is None:
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if is_bundle_uri(uri):
        if bundle_headline is None:
            return None
        data = read_bundle_entry(
            resolved, bundle_headline, max_bytes=_CONTENT_MAX_BYTES
        )
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if size > _CONTENT_MAX_BYTES:
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_artifact_manifest(
    uri: str | None, artifacts_dir: Path
) -> dict | None:
    """Read the bundle manifest for ``uri`` iff it's an in-jail ``.tar.gz``."""
    if not is_bundle_uri(uri):
        return None
    resolved = _resolve_inside_jail(uri, artifacts_dir)
    if resolved is None:
        return None
    return read_bundle_manifest(resolved)


def read_idea_content(
    idea: Idea, artifacts_dir: Path
) -> str | None:
    """Return the content text iff the artifact is safely confined.

    For ``.tar.gz`` bundles, returns the ``content.md`` headline entry
    if present; otherwise ``None`` (the manifest table still
    renders, supplied by :func:`read_idea_manifest`).
    """
    return _read_inline_artifact(
        idea.artifacts_uri,
        artifacts_dir,
        bundle_headline=IDEA_BUNDLE_HEADLINE,
    )


def read_idea_manifest(
    idea: Idea, artifacts_dir: Path
) -> dict | None:
    """Return the manifest dict iff the idea's artifact is a bundle."""
    return _read_artifact_manifest(idea.artifacts_uri, artifacts_dir)


def read_variant_artifact(
    artifacts_uri: str | None, artifacts_dir: Path
) -> str | None:
    """Return the variant's inline artifact text iff safely confined.

    Sibling to :func:`read_idea_content` for the
    chunk-9d evaluator draft view; ``variant.artifacts_uri`` is
    optional and may be ``None``, which short-circuits to ``None``.
    """
    return _read_inline_artifact(
        artifacts_uri,
        artifacts_dir,
        bundle_headline=VARIANT_BUNDLE_HEADLINE,
    )


def read_variant_artifact_manifest(
    artifacts_uri: str | None, artifacts_dir: Path
) -> dict | None:
    """Return the manifest dict iff the variant's artifact is a bundle."""
    return _read_artifact_manifest(artifacts_uri, artifacts_dir)


def read_evaluation_artifact(
    artifacts_uri: str | None, artifacts_dir: Path
) -> str | None:
    """Return the evaluator's inline artifact text iff safely confined."""
    return _read_inline_artifact(
        artifacts_uri,
        artifacts_dir,
        bundle_headline=EVALUATION_BUNDLE_HEADLINE,
    )


def read_evaluation_artifact_manifest(
    artifacts_uri: str | None, artifacts_dir: Path
) -> dict | None:
    """Return the manifest dict iff the evaluator artifact is a bundle."""
    return _read_artifact_manifest(artifacts_uri, artifacts_dir)


# ----------------------------------------------------------------------
# Issue #137 — pending-task list redesign (executor + evaluator).
#
# All list-page state (sort / direction / filter / group) is driven by
# URL query params (decision §2.2 — stateless, shareable; no per-click
# cookie rewrite). Every param value is run through an allow-list before
# it reaches the page so an attacker-supplied ``?sort=`` can never reflect
# into a link href (§D.2 query-param-injection risk).
# ----------------------------------------------------------------------

#: The two sortable columns. ``created_at`` is the always-on implicit
#: secondary tiebreak (§D.2), not a user-selectable axis.
SORT_KEYS: tuple[str, ...] = ("priority", "slug")
SORT_DIRECTIONS: tuple[str, ...] = ("asc", "desc")
TARGET_FILTERS: tuple[str, ...] = ("all", "targeted", "untargeted")


@dataclass(frozen=True)
class ListView:
    """Validated, allow-listed view state for a pending-task list page."""

    sort: str  # one of SORT_KEYS
    direction: str  # one of SORT_DIRECTIONS
    eligible_only: bool
    target_filter: str  # one of TARGET_FILTERS
    group_by_creator: bool


def _default_direction(sort: str) -> str:
    """Default sort direction for a column when none is supplied.

    Priority defaults to DESC (highest-promise first, the §D.2 default);
    slug defaults to ASC (natural alphabetic).
    """
    return "desc" if sort == "priority" else "asc"


def parse_list_view(query_params: Any) -> ListView:
    """Parse list-page query params into an allow-listed :class:`ListView`.

    Unknown or absent values fall back to the defaults; no raw input
    is ever carried forward (§D.2). ``query_params`` is the Starlette
    ``request.query_params`` multidict (``.get`` returns ``str | None``).
    """
    sort = query_params.get("sort")
    if sort not in SORT_KEYS:
        sort = "priority"
    direction = query_params.get("dir")
    if direction not in SORT_DIRECTIONS:
        direction = _default_direction(sort)
    target_filter = query_params.get("target")
    if target_filter not in TARGET_FILTERS:
        target_filter = "all"
    # "Eligible for me" defaults ON (§D.4); only an explicit "0" turns
    # it off. group-by-creator defaults OFF; only explicit "1" turns on.
    eligible_only = query_params.get("eligible", "1") != "0"
    group_by_creator = query_params.get("group", "0") == "1"
    return ListView(
        sort=sort,
        direction=direction,
        eligible_only=eligible_only,
        target_filter=target_filter,
        group_by_creator=group_by_creator,
    )


def build_list_links(view: ListView) -> dict[str, Any]:
    """Build every list-page link href from the validated ``view``.

    All hrefs are constructed from allow-listed values only — raw
    query input is never echoed back into markup (§D.2). The template
    renders these verbatim, so it needs no query-string logic.
    """

    def href(**overrides: str) -> str:
        params = {
            "sort": view.sort,
            "dir": view.direction,
            "eligible": "1" if view.eligible_only else "0",
            "target": view.target_filter,
            "group": "1" if view.group_by_creator else "0",
        }
        params.update(overrides)
        return "?" + urlencode(params)

    def sort_href(col: str) -> str:
        if view.sort == col:
            # Clicking the active column flips its direction.
            new_dir = "asc" if view.direction == "desc" else "desc"
        else:
            new_dir = _default_direction(col)
        return href(sort=col, dir=new_dir)

    return {
        "sort": {
            col: {
                "href": sort_href(col),
                "active": view.sort == col,
                "direction": view.direction if view.sort == col else None,
            }
            for col in SORT_KEYS
        },
        "eligible_toggle": href(eligible="0" if view.eligible_only else "1"),
        "target": {f: href(target=f) for f in TARGET_FILTERS},
        "group_toggle": href(group="0" if view.group_by_creator else "1"),
    }


class EligibilityResolver:
    """Resolve per-row claim eligibility, memoizing group walks per render.

    Mirrors the store's §3.5 claim ladder (registration first, then
    target) as an *advisory* projection — the claim write remains the
    enforcement point (§5). Registration is read once per render
    (``read_worker``); group membership is walked at most once per
    ``group_id`` (decision §2.3) so N group-targeted rows against the
    same group cost one DAG walk, not N.

    Three-outcome discipline (AGENTS.md "narrow exception handling"):

    - ``read_worker`` succeeds → registered.
    - ``read_worker`` raises ``NotFound`` → not registered; every row
      is ineligible (the §3.5 step-2 ``WorkerNotRegistered`` outcome).
    - ``read_worker`` raises anything else (transport / auth) →
      registration is *unknown for the whole page*; every row renders
      as eligibility-unknown and no group probe runs.

    Per-row group resolution distinguishes the same three outcomes:
    ``resolve_worker_in_group`` returns ``False`` (not raises) for an
    unknown worker/group, so only a transport-shaped exception marks a
    row unknown — a real ``False`` is a legitimate "not eligible".
    """

    def __init__(self, store: Any, worker_id: str) -> None:
        self._store = store
        self._worker_id = worker_id
        self._group_cache: dict[str, bool] = {}
        self.registered = False
        self.registration_unknown = False
        #: Count of rows whose eligibility could not be resolved due to a
        #: transport-shaped failure (the §D.6 second warning counter).
        self.unresolved_count = 0
        self._resolve_registration()

    def _resolve_registration(self) -> None:
        try:
            self._store.read_worker(self._worker_id)
        except StorageNotFound:
            self.registered = False
        except Exception:  # noqa: BLE001 — transport / auth-shaped
            self.registration_unknown = True
        else:
            self.registered = True
        if self.registration_unknown:
            # Page-level: bump the eligibility counter once (§D.4), not
            # per row — there is a single underlying registration read.
            self.unresolved_count += 1

    def resolve(self, target: Any) -> tuple[bool, bool]:
        """Return ``(eligible, unknown)`` for a task ``target``.

        ``unknown`` is ``True`` only when a transport-indeterminate
        failure prevented resolution; the caller renders such rows with
        a disabled claim button + an "eligibility unknown" note.
        """
        if self.registration_unknown:
            return False, True
        if not self.registered:
            return False, False
        if target is None:
            return True, False
        if target.kind == "worker":
            return target.id == self._worker_id, False
        if target.kind == "group":
            return self._resolve_group(target.id)
        return False, False

    def _resolve_group(self, group_id: str) -> tuple[bool, bool]:
        """Return ``(eligible, unknown)`` for a group target, memoized.

        A transport-shaped failure is the only ``unknown`` path; a real
        ``False`` from ``resolve_worker_in_group`` (unknown worker /
        dangling group ref) is a legitimate "not a member".
        """
        if group_id in self._group_cache:
            return self._group_cache[group_id], False
        try:
            result = self._store.resolve_worker_in_group(
                self._worker_id, group_id
            )
        except Exception:  # noqa: BLE001 — transport / auth-shaped
            self.unresolved_count += 1
            return False, True
        self._group_cache[group_id] = result
        return result, False


def build_artifact_links(
    uri: str | None, artifacts_dir: Path
) -> dict[str, Any]:
    """Build the expansion's artifact "view content" links for ``uri``.

    Returns a dict the template renders without further logic (§D.3):

    - ``{"kind": "none"}`` — no artifacts_uri set.
    - ``{"kind": "bundle", "entries": [<path>, ...], "uri": uri}`` — an
      in-jail ``.tar.gz``; one ``GET /artifacts?uri=&entry=`` link per
      manifest entry.
    - ``{"kind": "file", "uri": uri}`` — a non-bundle in-scope ``file://``
      URI; a single ``GET /artifacts?uri=`` link (no ``entry``). This
      branch must not be dropped — many artifacts are single files.
    - ``{"kind": "unreadable"}`` — a ``.tar.gz`` whose manifest could not
      be read (renders "(artifacts unavailable)").
    - ``{"kind": "external", "uri": uri}`` — an ``http(s)://`` URI; a
      direct external link (parity with the admin detail templates).
    - ``{"kind": "opaque", "uri": uri}`` — any other scheme; rendered as
      plain text.

    The template URL-encodes ``uri`` / ``entry`` with the ``urlencode``
    filter, mirroring the admin detail templates.
    """
    if not uri:
        return {"kind": "none"}
    scheme = urlparse(uri).scheme.lower()
    if scheme != "file":
        if scheme in ("http", "https"):
            return {"kind": "external", "uri": uri}
        return {"kind": "opaque", "uri": uri}
    if is_bundle_uri(uri):
        manifest = _read_artifact_manifest(uri, artifacts_dir)
        if manifest is None:
            return {"kind": "unreadable"}
        entries = [
            str(e["path"])
            for e in manifest.get("entries", [])
            if isinstance(e, dict) and e.get("path")
        ]
        return {"kind": "bundle", "entries": entries, "uri": uri}
    return {"kind": "file", "uri": uri}


def _row_is_present(row: dict[str, Any]) -> bool:
    """A row is *present* (sortable) iff its idea resolved (slug/priority set)."""
    return row.get("slug") is not None and row.get("priority") is not None


def _filter_pending_rows(
    rows: list[dict[str, Any]], view: ListView
) -> list[dict[str, Any]]:
    """Apply the eligibility + target-tristate filters (§D.4).

    The "eligible for me" filter removes only rows that are *definitively*
    ineligible. Rows whose eligibility could not be resolved
    (``eligibility_unknown`` — a registration- or group-probe transport
    failure) stay visible, rendered with a disabled claim button, so a
    transient outage never silently hides claimable work (§D.4).
    """
    out = rows
    if view.eligible_only:
        out = [
            r for r in out if r.get("eligible") or r.get("eligibility_unknown")
        ]
    if view.target_filter == "targeted":
        out = [r for r in out if r["task"].target is not None]
    elif view.target_filter == "untargeted":
        out = [r for r in out if r["task"].target is None]
    return out


def _sort_pending_rows(
    rows: list[dict[str, Any]], view: ListView
) -> list[dict[str, Any]]:
    """Sort present rows by the chosen axis; degraded rows sink to the bottom.

    Direction-safe degraded placement (§D.2): present rows (idea
    resolved) are sorted; degraded rows (idea unavailable / read-failed)
    are concatenated *after*, in stable original order, for both
    directions — achieved by partitioning, not a ``-inf``/``""`` sentinel
    (which would float degraded rows to the top under ascending sorts).

    ``created_at`` is the always-on secondary tiebreak, applied ASC
    regardless of the primary direction: we sort by it first (stable),
    then re-sort by the primary key — Python's stable sort preserves the
    tiebreak order within equal-primary runs even when ``reverse=True``.
    """
    present = [r for r in rows if _row_is_present(r)]
    degraded = [r for r in rows if not _row_is_present(r)]
    present.sort(key=lambda r: r["task"].created_at)
    reverse = view.direction == "desc"
    present.sort(key=lambda r: r[view.sort], reverse=reverse)
    return present + degraded


def _group_rows_by_creator(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group already-sorted rows by ``created_by``, preserving order.

    Returns ``[{"created_by": <id|None>, "rows": [...]}, ...]``; the
    first appearance of each creator fixes the group order so the sort
    above carries through.
    """
    groups: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = row.get("created_by")
        if key not in groups:
            groups[key] = {"created_by": key, "rows": []}
        groups[key]["rows"].append(row)
    return list(groups.values())


def arrange_pending_rows(
    rows: list[dict[str, Any]], view: ListView
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Filter → sort → optionally group the pending rows for rendering.

    Returns ``(flat_rows, groups)``: ``flat_rows`` is always the
    filtered+sorted list; ``groups`` is the grouped-by-creator structure
    when ``view.group_by_creator`` is set, else ``None``.
    """
    arranged = _sort_pending_rows(_filter_pending_rows(rows, view), view)
    groups = _group_rows_by_creator(arranged) if view.group_by_creator else None
    return arranged, groups
