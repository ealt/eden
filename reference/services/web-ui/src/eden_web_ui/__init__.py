"""Reference Web UI service.

Phase 9 chunk 1: shell + planner module. Server-rendered Jinja
templates over a FastAPI app. Backend-for-frontend: holds the
shared bearer, talks to the task-store-server only via
``eden_wire.StoreClient``, exposes only HTML to the browser.
"""

from .app import make_app

__all__ = ["make_app"]
