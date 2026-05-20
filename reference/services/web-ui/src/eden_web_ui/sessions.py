"""Signed session-cookie + CSRF helpers.

Cookie layout: ``itsdangerous`` URL-safe-serialized JSON with two
fields — ``worker_id`` (the value passed to ``Store.claim``) and
``csrf`` (a per-session random token compared in constant time
against a hidden ``csrf_token`` form field on every mutating route).

The session secret is set at app construction time and never
exposed in any response. Restarting the service with a fresh
secret invalidates outstanding sessions.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeSerializer

SESSION_COOKIE_NAME = "eden_web_ui_session"


@dataclass(frozen=True)
class Session:
    """Decoded session-cookie contents.

    ``selected_experiment_id`` (12c wave 5) — the experiment the
    operator has selected via the cross-experiment switcher. When
    ``None`` (default for pre-12c sessions and the no-control-plane
    posture), routes use the deployment's default experiment.
    """

    worker_id: str
    csrf: str
    selected_experiment_id: str | None = None


class SessionCodec:
    """Serializer/deserializer for the session cookie."""

    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeSerializer(secret, salt="eden-web-ui-session")

    def encode(self, session: Session) -> str:
        """Sign + url-encode ``session`` to a cookie-safe string."""
        payload: dict[str, str] = {
            "worker_id": session.worker_id,
            "csrf": session.csrf,
        }
        if session.selected_experiment_id is not None:
            payload["selected_experiment_id"] = session.selected_experiment_id
        return self._serializer.dumps(payload)

    def decode(self, raw: str) -> Session | None:
        """Verify ``raw`` and return the decoded session, or ``None`` on bad signature/shape."""
        try:
            data = self._serializer.loads(raw)
        except BadSignature:
            return None
        if not isinstance(data, dict):
            return None
        worker_id = data.get("worker_id")
        csrf = data.get("csrf")
        if not isinstance(worker_id, str) or not isinstance(csrf, str):
            return None
        selected = data.get("selected_experiment_id")
        if selected is not None and not isinstance(selected, str):
            return None
        return Session(
            worker_id=worker_id,
            csrf=csrf,
            selected_experiment_id=selected,
        )


def new_csrf_token() -> str:
    """Generate a fresh per-session CSRF token."""
    return secrets.token_urlsafe(32)


def verify_csrf(session: Session, presented: str | None) -> bool:
    """Constant-time compare a presented CSRF token against the session's."""
    if presented is None:
        return False
    return hmac.compare_digest(session.csrf, presented)
