# eden-task-store-server

Reference implementation of the EDEN task-store process. Hosts a `Store` (in-memory, SQLite, or Postgres) behind the FastAPI app from [`eden-wire`](../../packages/eden-wire/) and exposes it over the spec-chapter-07 HTTP binding.

## Run

```bash
python -m eden_task_store_server \
  --store-url /tmp/eden.sqlite \
  --experiment-id exp-1 \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml \
  --host 127.0.0.1 --port 8080
```

`--store-url` accepts:

- `:memory:` — in-memory `Store` (non-durable).
- `sqlite:///<relative-path>` or `sqlite:////<absolute-path>` —
  `SqliteStore`. (Note the **four** leading slashes for an absolute
  path: the SQLite URL scheme uses three for the URL prefix and one
  for the leading slash of the path.)
- A bare filesystem path (no scheme) is also accepted as
  `SqliteStore` — interpreted literally, so `/tmp/eden.sqlite` is
  unambiguous.
- `postgresql://<user>:<pw>@<host>:5432/<db>` — `PostgresStore`.

The deprecated `--db-path` alias (interpreted as a SQLite path or
`:memory:`) is kept for one phase; logs emit a deprecation warning
when it is used.

On startup the first stdout line is `EDEN_TASK_STORE_LISTENING host=<host> port=<port>` so a supervisor / test harness can read the ephemeral port (`--port 0` to bind any).

## Checkpoint-export repo

`--repo-path <dir>` names the local bare git repo the chapter-10 checkpoint
export bundles into every archive's `repo.bundle` (it also deepens the
chapter-3 §3.3 tree-identity check). Pair it with `--forgejo-url <url>`
(plus optional `--credential-helper <script>`) so each export first syncs
the clone from the deployment's git remote of record — clone on first
export, `fetch --prune` thereafter (issue
[#294](https://github.com/ealt/eden/issues/294)). The sync is lazy:
startup never touches the remote, so a checkpoint-import receiver runs
fine without its git remote up. A failed sync fails the export with 503
`eden://reference-error/checkpoint-repo-unavailable`.

## Auth

`--admin-token <T>` enables the normative §13 auth middleware from [`spec/v0/07-wire-protocol.md`](../../../spec/v0/07-wire-protocol.md): every `/v0/` request must carry `Authorization: Bearer <principal>:<secret>` (the literal `admin` principal matches this token; worker bearers verify against the Store). Without it, the server accepts anonymous requests (test / in-process posture only). The pre-12a-1 `--shared-token` scheme is retired.

## Shutdown

SIGTERM / SIGINT drain in-flight requests, then close the store.
