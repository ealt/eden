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

- `experiment` — `(experiment_id text, name text, evaluation_schema text)` — `experiment_id` is the opaque `exp_*` id; `name` is the optional operator-supplied display label (`NULL` when none was given), indexed for the `?name=` lookup path (issue [#128](https://github.com/ealt/eden/issues/128)).
- `task` — `(task_id text, kind text, state text, data text)`
- `submission` — `(task_id text, kind text, data text)`
- `idea` — `(idea_id text, state text, data text)`
- `variant` — `(variant_id text, status text, data text)`
- `event` — `(seq bigint, event_id text, type text, occurred_at text, experiment_id text, data text)`
- `worker_group` — `(group_id text, name text, data text)` — `group_id` is the opaque `grp_*` id; `name` is the optional display label (reserved groups carry `name == 'admins'` / `'orchestrators'`), indexed for `?name=` lookups (issue [#128](https://github.com/ealt/eden/issues/128)).
- `group_membership` — `(group_id text, member_id text, position integer)` — `member_id` is an opaque id (a `wkr_*` worker or a `grp_*` group).
- `schema_version` — migration bookkeeping

## 2. The `worker` table — column-projection required

The `worker` table is `(worker_id text, name text, data text,
credential_hash text)`. `worker_id` is the opaque `wkr_*` id; `name`
is the optional operator-supplied display label (`NULL` when none was
given), indexed for the `?name=` lookup path (issue
[#128](https://github.com/ealt/eden/issues/128)). The role has
**column-level** SELECT on `worker_id` + `name` + `data` only;
`credential_hash` is intentionally excluded (it carries the argon2id
hash of the per-worker bearer secret, which the role MUST NOT see).

PostgreSQL's permission check on `SELECT *` is against the full
set of columns the parser expands `*` into. If any column lacks
SELECT, the whole statement fails. The operator-visible posture:

| Query | Result |
|---|---|
| `SELECT worker_id, name, data FROM worker` | Works |
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
called `register_worker`. The optional display label is the
top-level `name` column (and is also mirrored in `data`); the
`registered_by` actor is an opaque `wkr_*` id or the literal
`admin`. Example:

```sql
SELECT
    worker_id,
    name,
    data->>'registered_at'                  AS registered_at,
    data->>'registered_by'                  AS registered_by,
    data->'labels'                          AS labels
FROM worker
ORDER BY data->>'registered_at';
```

## 5. The `variant_unpacked` convenience view

For casual exploration in Adminer or psql, the task-store-server
creates a Postgres view named `variant_unpacked` at startup that
unpacks the `variant.data` JSON blob into typed scalar columns
(issue #124). The base `variant` table is untouched; the view is a
read-only convenience layer.

Common columns (always present) cover every public field on the
[`Variant`](../../reference/packages/eden-contracts/src/eden_contracts/variant.py)
dataclass: `variant_id`, `status`, `experiment_id`, `idea_id`,
`branch`, `commit_sha`, `variant_commit_sha`, `parent_commits`
(JSONB array), `artifacts_uri`, `description`, `executed_by`,
`evaluated_by`, `started_at`, `completed_at`, and `evaluation`
(JSONB sub-object).

Per-metric columns are generated from the experiment's
`evaluation_schema`. Each declared metric becomes its own typed
column — `integer` → Postgres `integer`, `real` → Postgres
`double precision`, `text` → Postgres `text`. The
[`EvaluationSchema`](../../reference/packages/eden-contracts/src/eden_contracts/evaluation.py)
reserved-names check prevents collisions with the common-column
space.

The pre-view nested-JSON query:

```sql
SELECT
    variant_id,
    status,
    (data::jsonb -> 'evaluation' ->> 'correctness')::real AS correctness
FROM variant
WHERE status = 'success'
ORDER BY correctness DESC;
```

becomes:

```sql
SELECT variant_id, status, correctness
FROM variant_unpacked
WHERE status = 'success'
ORDER BY correctness DESC;
```

The view is dropped and recreated on every PostgresStore open so a
fresh experiment with a different `evaluation_schema` picks up the
new metric columns. The `eden_readonly` role has SELECT on the
view in addition to the underlying `variant` table.

## 6. Attribution joins across tables

The artifact tables carry redundant attribution fields per
chapter 02 §3.1 / §5.1 / §9 so an agent can join without going
through the worker registry. The attribution fields
(`evaluated_by`, `executed_by`, …) carry opaque `wkr_*` ids since
issue [#128](https://github.com/ealt/eden/issues/128); resolve a
display name to its id via the `worker` table's `name` column (names
MAY collide — 0..N rows). Example: "every variant the evaluator-host-1
worker has terminalized":

```sql
SELECT
    v.variant_id,
    v.status,
    v.data->>'evaluated_by' AS evaluated_by
FROM variant v
JOIN worker w ON w.worker_id = v.data->>'evaluated_by'
WHERE w.name = 'evaluator-host-1'
  AND v.status IN ('success', 'error', 'evaluation_error');
```

## 7. Privilege boundary

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

## 8. Password rotation

The task-store-server's `--readonly-password` rotates the role's
password via `ALTER ROLE … WITH PASSWORD …` on every startup.
The password is preserved across `setup-experiment.sh` re-runs
to avoid breaking live subprocesses; operators force rotation
by deleting the `EDEN_READONLY_PASSWORD` line from `.env` and
re-running setup-experiment. After a rotation, existing
subprocesses with the old DSN will start failing on connect —
restart the worker hosts (`docker compose restart ideator-host
evaluator-host`) so they pick up the new DSN.

## 9. Cross-machine

The DSN's `postgres:5432` host only resolves inside the compose
network. An off-host agent substitutes the operator's
externally-reachable Postgres hostname (the `eden_readonly`
role + password are unchanged). Phase 13c switches the
deployment to managed Postgres and ports the role concept
cleanly.
