#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the EDEN reference Compose infrastructure stack.
# Brings the stack up using .env.example as the source of truth (so
# CI does not mutate the checked-in file), waits for healthy, runs a
# few independent assertions, and tears the stack down on exit.

# Tooling preflight — fail fast with a clear message if a required
# binary is missing.
for tool in docker jq curl; do
    command -v "$tool" >/dev/null || {
        echo "smoke.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

cd "$(dirname "$0")/.."

TMPENV="$(mktemp)"
cp .env.example "$TMPENV"

# Tear the stack down BEFORE removing the env file — `docker compose
# down -v` needs --env-file to resolve the same project, so the order
# matters. Preserve the script's exit status across the cleanup.
cleanup() {
    local rc=$?
    docker compose -f compose.yaml --env-file "$TMPENV" down -v >/dev/null 2>&1 || true
    rm -f "$TMPENV"
    exit "$rc"
}
trap cleanup EXIT

echo "--- bringing up the stack ---"
docker compose -f compose.yaml --env-file "$TMPENV" up -d --wait --wait-timeout 120

echo "--- compose ps ---"
docker compose -f compose.yaml --env-file "$TMPENV" ps -a

# Resolve the project name from compose config and assert it matches
# the value pinned in compose.yaml. Project-prefixed volume names
# follow the standard "<project>_<volume>" convention; pinning the
# project name here means a future rename of `name:` fails the smoke
# test loudly rather than silently checking the wrong volumes.
PROJECT="$(docker compose -f compose.yaml --env-file "$TMPENV" \
            config --format json | jq -r '.name')"
test "$PROJECT" = "eden-reference" || {
    echo "unexpected project name: $PROJECT" >&2
    exit 1
}

echo "--- asserting all three named volumes exist ---"
for vol in eden-postgres-data eden-gitea-data eden-blob-data; do
    docker volume inspect "${PROJECT}_${vol}" >/dev/null
done

echo "--- asserting blob-init exited 0 ---"
# Using `docker inspect` against the explicit container_name avoids
# `docker compose ps --format json` shape variations across Compose
# versions (some emit NDJSON, some emit a JSON array).
test "$(docker inspect --format '{{.State.ExitCode}}' eden-blob-init)" = "0"

echo "--- asserting Postgres accepts connections ---"
# Read POSTGRES_USER / POSTGRES_DB / GITEA_HOST_PORT from $TMPENV
# rather than hardcoding so an .env.example bump propagates without
# silently breaking the smoke test.
PG_USER="$(grep -E '^POSTGRES_USER=' "$TMPENV" | cut -d= -f2-)"
PG_DB="$(grep -E '^POSTGRES_DB=' "$TMPENV" | cut -d= -f2-)"
docker compose -f compose.yaml --env-file "$TMPENV" exec -T postgres \
    pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null

echo "--- asserting Gitea API responds ---"
GITEA_PORT="$(grep -E '^GITEA_HOST_PORT=' "$TMPENV" | cut -d= -f2-)"
curl -fsS "http://localhost:${GITEA_PORT}/api/v1/version" | jq -e '.version' >/dev/null

echo "PASS"
