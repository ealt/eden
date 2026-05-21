# Agent substrate access — operator how-to (Phase 12a-1f)

This document walks an operator through writing an **agentic
ideator** or **agentic evaluator** that uses the three read-side
substrates Phase 12a-1f opens to subprocess workers:

- **Git** — the experiment's central repo, exposed as a local
  bare clone or over Forgejo HTTP.
- **Artifact server** — idea content + evaluator artifacts,
  served as bytes over a thin HTTP route on the task-store-server.
- **Postgres event log** — the experiment's state, exposed via a
  readonly role with column-level safety.

The substrates are **read-side** by design: an agent inspects the
experiment to decide what to do next, then submits its decision
through the normative wire (chapter 4 / 7). Writes still go
through the existing transactional API.

The contract for these substrates lives in
[`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
§9.

## 1. What the agent gets

Every spawned `ideation_command` / `evaluation_command`
subprocess receives these environment variables in addition to
the chunk-10d / 12a-1 baseline (`EDEN_TASK_JSON`, `EDEN_WORKER_ID`,
`EDEN_WORKER_CREDENTIAL`, etc.):

| Variable | Example value | Purpose |
|---|---|---|
| `EDEN_REPO_DIR` | `/var/lib/eden/repo` | Host-side path to the worker's bare git clone. `git -C $EDEN_REPO_DIR log --all --oneline` enumerates every ref the integrator has integrated. |
| `EDEN_ARTIFACT_URL` | `http://task-store-server:8080/_reference/experiments/exp-1/artifacts/` | HTTP base URL (ending `/`) for the artifact-server route. |
| `EDEN_ARTIFACT_PATH_ROOT` | `/var/lib/eden/artifacts` | Host-side filesystem root the URL is anchored at. Used by the agent to translate `file:///var/lib/eden/artifacts/foo.md` URIs from the wire into the relative path `foo.md` → fetch as `${EDEN_ARTIFACT_URL}foo.md`. |
| `EDEN_READONLY_STORE_URL` | `postgresql://eden_readonly:<pwd>@postgres:5432/eden` | Postgres DSN with read-only privileges. Connect with `psycopg`, `asyncpg`, or any client. |

The bearer for `EDEN_ARTIFACT_URL` is the per-worker bearer the
12a-1 binding already documents:

```python
import os
bearer = f"{os.environ['EDEN_WORKER_ID']}:{os.environ['EDEN_WORKER_CREDENTIAL']}"
```

## 2. Reading from each substrate

### 2.1 Git

```python
import os
import subprocess

repo_dir = os.environ["EDEN_REPO_DIR"]

# All variant refs and their tips:
result = subprocess.run(
    ["git", "-C", repo_dir, "for-each-ref",
     "--format=%(refname:short) %(objectname)",
     "refs/heads/variant/"],
    capture_output=True, text=True, check=True,
)
for line in result.stdout.splitlines():
    branch, sha = line.split(maxsplit=1)
    # ...

# Read the evaluation manifest at a variant's tip:
manifest = subprocess.run(
    ["git", "-C", repo_dir, "show",
     f"{sha}:.eden/variants/{variant_id}/evaluation.json"],
    capture_output=True, text=True, check=True,
).stdout
```

### 2.2 Artifact server

```python
import os
import httpx

base = os.environ["EDEN_ARTIFACT_URL"]
bearer = f"{os.environ['EDEN_WORKER_ID']}:{os.environ['EDEN_WORKER_CREDENTIAL']}"
path_root = os.environ["EDEN_ARTIFACT_PATH_ROOT"]

# An artifact URI from the wire (e.g. idea.artifacts_uri) is a
# file:// URI rooted at EDEN_ARTIFACT_PATH_ROOT. Translate to a
# URL by stripping the prefix and appending to EDEN_ARTIFACT_URL:
def to_http_url(file_uri: str) -> str:
    file_prefix = f"file://{path_root.rstrip('/')}/"
    assert file_uri.startswith(file_prefix), file_uri
    relative = file_uri[len(file_prefix):]
    return f"{base}{relative}"

response = httpx.get(
    to_http_url(idea.artifacts_uri),
    headers={"Authorization": f"Bearer {bearer}"},
)
response.raise_for_status()
content = response.text
```

**Response shape.** Every 200 carries
`Content-Type: application/octet-stream` +
`Content-Disposition: attachment` + `X-Content-Type-Options:
nosniff` — the route never serves browser-executable MIME types
(stored-XSS defense). The agent decodes via the `artifacts_uri`
domain knowledge it already has (e.g. "idea content is always
markdown").

**Cap.** Files larger than 1 MiB return 413
`eden://reference-error/artifact-too-large`. Phase 13d's
`Backend` abstraction will handle streaming + range requests
properly.

### 2.3 Postgres event log

```python
import os
import psycopg

dsn = os.environ["EDEN_READONLY_STORE_URL"]

with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        # Recent variant completions:
        cur.execute("""
            SELECT data
            FROM event
            WHERE type = 'variant.integrated'
            ORDER BY seq DESC
            LIMIT 50
        """)
        recent = [row[0] for row in cur.fetchall()]
```

**Schema reference.** See
[`agent-readonly-db.md`](agent-readonly-db.md) for the full
granted-table list, column shapes, and worked example queries.

**Column-projection requirement on `worker`.** The readonly role
has SELECT on `worker_id` + `data` only; the parser expands
`SELECT *` to all columns and fails when `credential_hash` is
missing. Always project explicitly:

```sql
-- Works:
SELECT worker_id, data FROM worker WHERE worker_id = 'evaluator-1';
-- Fails:
SELECT * FROM worker;
SELECT credential_hash FROM worker;
```

## 3. On-host (compose) vs off-host

The compose-internal defaults bake hostnames that only resolve
inside the worker-host container's network namespace
(`task-store-server:8080`, `postgres:5432`,
`/var/lib/eden/repo`). An agent running off-host (e.g. an LLM
client on a developer laptop) substitutes its own values.

| Substrate | On-host (compose) | Off-host (substitute) |
|---|---|---|
| Git | `EDEN_REPO_DIR=/var/lib/eden/repo` (bind-mount) | Clone `http://<your-forgejo-host>:<port>/eden/<id>.git` with operator-supplied credentials; set `EDEN_REPO_DIR` to that clone. Per-worker Forgejo tokens with branch ACLs are Phase 13e. |
| Artifact server | `EDEN_ARTIFACT_URL=http://task-store-server:8080/_reference/experiments/<id>/artifacts/` | Substitute `<your-reverse-proxy>/_reference/...`. The `EDEN_WORKER_CREDENTIAL` bearer is unchanged. |
| Postgres | `EDEN_READONLY_STORE_URL=postgresql://eden_readonly:<pwd>@postgres:5432/eden` | Substitute `<your-postgres-host>`. The `eden_readonly` role + password are unchanged. |

**`EDEN_ARTIFACT_PATH_ROOT` matters cross-host.** Both the
worker host and the task-store-server bind-mount
`eden-artifacts-data` at `/var/lib/eden/artifacts`, so on-host
the URI translation is mechanical. Off-host, the operator must
ensure their `EDEN_ARTIFACT_PATH_ROOT` matches the
task-store-server's `--artifacts-dir`, since the agent strips
the prefix from `file://` URIs to compute the URL suffix.

## 4. DooD-mode caveat (`--exec-mode docker`)

When the worker host runs in `--exec-mode docker` (chunk-10d
follow-up A), the user's `*_command` runs inside a sibling
docker container that the host docker daemon starts. Sibling
containers are NOT attached to the compose project network by
default, so `task-store-server:8080` / `postgres:5432` do not
resolve from inside them.

To avoid shipping a broken half-enabled surface, the ideator +
evaluator host CLI **suppresses** all four substrate env vars
in DooD mode and logs a WARN line at startup. Operators who
need DooD-mode substrate access wait for the follow-on
sub-chunk (12a-1f-followup-A) that adds `--network` plumbing
to the wrap.

## 5. Trust boundary

The readonly substrate intentionally exposes attribution
fields. An agent with the DSN can enumerate every worker in the
experiment by reading the `worker` table or by aggregating
`submitted_by` / `executed_by` / `evaluated_by` / `created_by`
across the artifact tables. This is by design — exploratory
reads need attribution — but operators who don't want this
surface should NOT enable the substrate (omit
`EDEN_READONLY_STORE_URL` from the host's environment, or run
the host without the `--readonly-store-url` flag).

The 1 MiB artifact cap is a conservative safety bound, not a
performance limit. Today's idea content is at most a few KB; if
an operator's artifacts legitimately exceed 1 MiB, the answer
is to wait for Phase 13d's `Backend` abstraction (or stand up
the operator's own static file server alongside; the agent's
`EDEN_ARTIFACT_URL` can point anywhere).
