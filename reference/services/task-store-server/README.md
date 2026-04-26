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

## Auth

`--shared-token <T>` enables the reference-only bearer-token middleware from [`spec/v0/07-wire-protocol.md`](../../../spec/v0/07-wire-protocol.md) §12. Without it, the server accepts anonymous requests.

## Shutdown

SIGTERM / SIGINT drain in-flight requests, then close the store.
