No findings.

The CSRF hardening is appropriate. `_csrf_failure_response()` now adds `HX-Reswap: none` for HTMX requests in [planner.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:460), and the new [test_add_row_htmx_csrf_failure_sets_hx_reswap_none](/Users/ericalt/Documents/eden/reference/services/web-ui/tests/test_planner_flow.py:191) pins the exact behavior you wanted. The non-HTMX `403` path is still intact, and broadening the helper so `claim`/`submit` also get the header on any future HTMX usage is fine.

I don’t see anything else in the HTMX vendor + redirect/error-branch hardening scope. I reran `uv run pytest -q reference/services/web-ui/tests/test_planner_flow.py -q`; it passed. You can consider this thread done.