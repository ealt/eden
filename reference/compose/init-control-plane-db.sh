#!/bin/sh
# Postgres init hook (issue #147): create the control-plane database.
#
# Mounted into /docker-entrypoint-initdb.d/ on the upstream
# postgres:16-alpine image. That directory's *.sh files run exactly
# ONCE, on a fresh data dir, after the server is up and $POSTGRES_DB
# has been created — but POSTGRES_DB only ever creates ONE database,
# so the control-plane's second logical database (chapter 11 §3.4
# Option A) needs an explicit CREATE here.
#
# POSIX sh (the alpine image's /bin/sh is busybox ash; no bashisms).
set -eu

DB_NAME="${POSTGRES_DB_CONTROL_PLANE:-eden_control_plane}"

# CREATE DATABASE has no IF NOT EXISTS; gate on a catalog lookup so a
# re-run (e.g. a future image that re-runs the hook) is idempotent
# rather than erroring the whole init.
exists="$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")"
if [ "$exists" = "1" ]; then
    echo "init-control-plane-db: database ${DB_NAME} already exists; skipping"
else
    echo "init-control-plane-db: creating database ${DB_NAME}"
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -c "CREATE DATABASE \"${DB_NAME}\" OWNER \"$POSTGRES_USER\";"
fi
