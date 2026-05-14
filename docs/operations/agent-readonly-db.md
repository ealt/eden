# Readonly Postgres substrate — schema reference (Phase 12a-1f)

The `eden_readonly` Postgres role provisioned by the
task-store-server (when started with `--readonly-password` against
a Postgres backend) has SELECT access to the experiment's event
log and artifact tables. This document is the operator-facing
reference for what's queryable and how.

The role is configured via REVOKE-then-GRANT in
[`ensure_readonly_role`](../../reference/packages/eden-storage/src/eden_storage/postgres.py)
and is intentionally **scoped to the live connection's
database + schema** (resolved via `current_database()` /
`current_schema()`, not hard-coded `eden` / `public`) so the
helper works against both production deployments and the
parametrized backend tests.

## 1. Granted tables

The role gets full-table `SELECT` on these tables in the active
schema:

- `experiment` — `(experiment_id text, evaluation_schema text)`
- `task` — `(task_id text, kind text, state text, data text)`
- `submission` — `(task_id text, kind text, data text)`
- `idea` — `(idea_id text, state text, data text)`
- `variant` — `(variant_id text, status text, data text)`
- `event` — `(seq bigint, event_id text, type text, occurred_at text, experiment_id text, data text)`
- `worker_group` — `(group_id text, data text)`
- `group_membership` — `(group_id text, member_id text, position integer)`
- `schema_version` — migration bookkeeping

## 2. The `worker` table — column-projection required

The `worker` table is `(worker_id text, data text,
credential_hash text)`. The role has **column-level** SELECT on
`worker_id` + `data` only; `credential_hash` is intentionally
excluded (it carries the argon2id hash of the per-worker bearer
secret, which the role MUST NOT see).

PostgreSQL's permission check on `SELECT *` is against the full
set of columns the parser expands `*` into. If any column lacks
SELECT, the whole statement fails. The operator-visible posture:

| Query | Result |
|---|---|
| `SELECT worker_id, data FROM worker` | Works |
| `SELECT * FROM worker` | Fails — "permission denied for column credential_hash" |
| `COPY worker TO STDOUT` | Fails (same reason) |
| `pg_dump --table=worker` | Fails (same reason) |
| `SELECT credential_hash FROM worker` | Fails (direct reference) |

**Queries against the `worker` table MUST project columns
explicitly.** The protection is asymmetric to the rest of the
schema, where `SELECT *` works fine.

## 3. JSON `data` column shapes

Most tables store their row payload in a `data` text column
containing the JSON-serialized model dump. The shapes mirror the
Pydantic models in
[`eden_contracts`](../../reference/packages/eden-contracts/src/eden_contracts/);
the readonly substrate is a thin view onto the same JSON, so the
schema does not duplicate it here.

Useful extractors:

```sql
-- Idea slug + parent commits:
SELECT
    idea_id,
    data->>'slug'           AS slug,
    data->>'priority'       AS priority,
    data->'parent_commits'  AS parent_commits
FROM idea
WHERE state = 'dispatched';

-- Variant attribution:
SELECT
    variant_id,
    status,
    data->>'commit_sha'   AS commit_sha,
    data->>'executed_by'  AS executed_by,
    data->>'evaluated_by' AS evaluated_by
FROM variant
WHERE status = 'success'
ORDER BY (data->>'created_at') DESC
LIMIT 20;

-- Most recent integration events:
SELECT
    event_id,
    occurred_at,
    data
FROM event
WHERE type = 'variant.integrated'
ORDER BY seq DESC
LIMIT 50;
```

The PostgreSQL JSON operators (`->`, `->>`, `#>`) work on the
`text` column transparently when cast (`data::jsonb`) or against
the text directly via `->>`. The reference impl does not
maintain a `jsonb` column — the on-disk shape stays `text` for
parity with the SQLite + in-memory backends.

## 4. Worker registration metadata

The worker `data` column carries the per-worker labels,
registration timestamp, and the actor (`registered_by`) that
called `register_worker`. Example:

```sql
SELECT
    worker_id,
    data->>'registered_at'                  AS registered_at,
    data->>'registered_by'                  AS registered_by,
    data->'labels'                          AS labels
FROM worker
ORDER BY data->>'registered_at';
```

## 5. Attribution joins across tables

The artifact tables carry redundant attribution fields per
chapter 02 §3.1 / §5.1 / §9 so an agent can join without going
through the worker registry. Example: "every variant evaluator-1
has terminalized":

```sql
SELECT
    variant_id,
    status,
    data->>'evaluated_by' AS evaluated_by
FROM variant
WHERE data->>'evaluated_by' = 'evaluator-1'
  AND status IN ('success', 'error', 'evaluation_error');
```

## 6. Privilege boundary

The role has **NO** privilege to:

- INSERT / UPDATE / DELETE on any granted table.
- CREATE / DROP / ALTER any table.
- Read `worker.credential_hash` directly OR via `SELECT *`.
- Be auto-granted SELECT on a future table — every schema bump
  that adds a new table must extend the GRANT list in
  `ensure_readonly_role` explicitly. This is deliberate
  (`ALTER DEFAULT PRIVILEGES` is intentionally absent) so a
  future credential-bearing column can't be accidentally
  exposed.

## 7. Password rotation

The task-store-server's `--readonly-password` rotates the role's
password via `ALTER ROLE … WITH PASSWORD …` on every startup.
The password is preserved across `setup-experiment.sh` re-runs
to avoid breaking live subprocesses; operators force rotation
by deleting the `EDEN_READONLY_PASSWORD` line from `.env` and
re-running setup-experiment. After a rotation, existing
subprocesses with the old DSN will start failing on connect —
restart the worker hosts (`docker compose restart ideator-host
evaluator-host`) so they pick up the new DSN.

## 8. Cross-machine

The DSN's `postgres:5432` host only resolves inside the compose
network. An off-host agent substitutes the operator's
externally-reachable Postgres hostname (the `eden_readonly`
role + password are unchanged). Phase 13c switches the
deployment to managed Postgres and ports the role concept
cleanly.
