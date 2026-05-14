**Findings**
1. Medium — **§D.11 reintroduces the simplified 12a-2 migration story that §§D.10/8.1 just corrected.**  
   §D.11 says: “**The `admin_store` construction in `cli.py` is one location; 12a-2 swaps the bearer.**” But §D.10 Alt C says the migration is “**a meaningful sub-chunk**” requiring “**(1) shipping per-session worker bearers … (2) updating the route layer … (3) adding the client-side check**,” and §8.1 repeats that it is “**not a simple bearer swap**.” Those cannot both be true. This is now the main remaining completeness issue: D.11 should use the same, corrected migration description as D.10/§8.1.

2. Medium — **The auth-model correction is still not fully accurate: the plan still says the session cookie carries `expires_at`.**  
   In §D.10 Alt C, the cookie is described as “**`{worker_id, csrf, expires_at}`**,” and §8.1 repeats “**the cookie payload is `{worker_id, csrf, expires_at}` only**.” The shipped code in [sessions.py](/Users/ericalt/Documents/eden-worktrees/phase-12a-1b-worker-group-admin-ui/reference/services/web-ui/src/eden_web_ui/sessions.py:3) stores only `worker_id` and `csrf`. This is no longer a bearer-model error, but it is still factual drift in exactly the area the plan is relying on for future RBAC reasoning.

3. Medium — **The disabled-state UI shape is still internally inconsistent.**  
   §D.3 says postures B/C “**render a placeholder page (`admin_workers_disabled.html` / `admin_groups_disabled.html`)**,” §D.7 lists those templates, and §5.1 includes them in the file inventory. But §D.4/§D.5/§6.7 describe a different implementation shape: the normal pages still render, with the register form or controls shown in a disabled state, and mutating POSTs “**redirect with `?error=admin-disabled`**.” Those are two different designs. The plan should pick one concrete shape so the template list, route behavior, and tests all line up.

**Overall Assessment**
No remaining critical feasibility blockers stood out. The plan’s core approach now matches the shipped 12a-1 wire surface and admin gating: dual clients, admin-token writes, idempotent worker registration, and idempotent absent-member removal are all correctly reflected.

What remains is mostly plan consistency, not core design risk. If you fix the D.11 migration wording, remove the stray `expires_at` claims, and resolve the disabled-template vs inline-disabled-page shape, the plan looks ready to execute.