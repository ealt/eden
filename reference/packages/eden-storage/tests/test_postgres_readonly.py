"""Tests for ``ensure_readonly_role`` (12a-1f §D.3.a / §D.3.b / §6.5).

Gated on ``EDEN_TEST_POSTGRES_DSN``. CI's ``python-test-postgres``
job spins up a postgres service container and exports the var;
without it these tests skip.

Verifies:

- First-run create + GRANT.
- Idempotent re-run.
- Password rotation (psycopg.OperationalError on old password).
- INSERT / UPDATE / DELETE on every granted table fail with
  permission denied.
- DDL (CREATE / DROP / ALTER) fails.
- ``SELECT * FROM worker`` fails (parser expands ``*`` to include
  the excluded ``credential_hash`` column).
- ``SELECT credential_hash FROM worker`` fails (direct column
  reference).
- ``SELECT worker_id, data FROM worker`` succeeds.
- Hardening against legacy over-grant: pre-provision with
  ``GRANT SELECT ON ALL TABLES`` + ``ALTER DEFAULT PRIVILEGES``,
  re-provision, confirm ``credential_hash`` is no longer
  reachable.
- A freshly-created table is NOT auto-readable by the readonly
  role (no ``ALTER DEFAULT PRIVILEGES`` installed).
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")

from eden_contracts import EvaluationSchema  # noqa: E402
from eden_storage import PostgresStore, ensure_readonly_role  # noqa: E402
from psycopg import sql  # noqa: E402

_DSN_ENV = os.environ.get("EDEN_TEST_POSTGRES_DSN")
if not _DSN_ENV:
    pytest.skip("EDEN_TEST_POSTGRES_DSN not set", allow_module_level=True)
# The module-level skip above narrows _DSN_ENV to non-None;
# pyright doesn't propagate that narrowing past the conditional,
# so we bind a strictly-typed alias here.
DSN: str = _DSN_ENV


@pytest.fixture
def schema_dsn() -> Iterator[tuple[str, str]]:
    """Provision a fresh per-test schema; yield (schema_name, dsn).

    The DSN has the schema pinned on ``search_path``. Schema is
    dropped on teardown.
    """
    schema = f"test_ro_{secrets.token_hex(8)}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    sep = "&" if "?" in DSN else "?"
    scoped_dsn = f"{DSN}{sep}options=-c%20search_path%3D{schema}"
    try:
        yield schema, scoped_dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as drop:
            drop.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        # Drop the readonly role. ``DROP OWNED BY`` clears any
        # database-level grants (CONNECT, default privileges) that
        # would otherwise leave the role un-droppable. Wrapped in
        # try/except because a test that skipped role-creation
        # would have no role to drop.
        with psycopg.connect(DSN, autocommit=True) as cleanup, cleanup.cursor() as cur:
            with contextlib.suppress(psycopg.errors.UndefinedObject):
                cur.execute("DROP OWNED BY eden_readonly")
            cur.execute("DROP ROLE IF EXISTS eden_readonly")


@pytest.fixture
def store_dsn(schema_dsn: tuple[str, str]) -> tuple[str, str]:
    """Initialize a PostgresStore against the test schema."""
    schema, scoped_dsn = schema_dsn
    store = PostgresStore(
        experiment_id="exp-ro-test",
        dsn=scoped_dsn,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )
    store.close()
    return schema, scoped_dsn


def _provision(store_dsn: tuple[str, str], password: str) -> None:
    """Run ensure_readonly_role against the test schema's DSN."""
    _, dsn = store_dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        ensure_readonly_role(conn, password=password)


def _readonly_dsn(store_dsn: tuple[str, str], password: str) -> str:
    """Return a DSN that connects as the readonly role."""
    schema, _ = store_dsn
    # Strip user/password from the configured DSN, substitute the
    # readonly role's credentials, and pin the schema.
    parsed = psycopg.conninfo.conninfo_to_dict(DSN)
    parsed["user"] = "eden_readonly"
    parsed["password"] = password
    parsed["options"] = f"-c search_path={schema}"
    return psycopg.conninfo.make_conninfo(**parsed)


# ----------------------------------------------------------------------
# Provisioning basics
# ----------------------------------------------------------------------


def test_first_run_creates_role_and_grants(
    store_dsn: tuple[str, str],
) -> None:
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn, conn.cursor() as cur:
        # SELECT on an unconditional table succeeds.
        cur.execute(f"SELECT COUNT(*) FROM {schema}.event")
        assert cur.fetchone()[0] >= 0


def test_idempotent_rerun(store_dsn: tuple[str, str]) -> None:
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    _provision(store_dsn, pwd)
    # Connect successfully after re-run.
    with psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_password_rotation_old_fails_new_works(
    store_dsn: tuple[str, str],
) -> None:
    pwd_a = secrets.token_hex(16)
    pwd_b = secrets.token_hex(16)
    _provision(store_dsn, pwd_a)
    _provision(store_dsn, pwd_b)
    # Old password now fails.
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_readonly_dsn(store_dsn, pwd_a)).close()
    # New password succeeds.
    with psycopg.connect(_readonly_dsn(store_dsn, pwd_b)) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


# ----------------------------------------------------------------------
# Privilege boundary
# ----------------------------------------------------------------------


def test_insert_into_granted_tables_fails(
    store_dsn: tuple[str, str],
) -> None:
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(
            f"INSERT INTO {schema}.idea (idea_id, state, data) "
            f"VALUES ('ro-test', 'drafting', '{{}}')"
        )


def test_update_fails(store_dsn: tuple[str, str]) -> None:
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"UPDATE {schema}.task SET state = 'dummy'")


def test_delete_fails(store_dsn: tuple[str, str]) -> None:
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"DELETE FROM {schema}.event")


def test_ddl_fails(store_dsn: tuple[str, str]) -> None:
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"CREATE TABLE {schema}.test_drift (id int NOT NULL)")


# ----------------------------------------------------------------------
# Column-level exclusion (load-bearing)
# ----------------------------------------------------------------------


def test_select_credential_hash_fails(
    store_dsn: tuple[str, str],
) -> None:
    """Direct reference to the excluded column → permission denied."""
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"SELECT credential_hash FROM {schema}.worker LIMIT 1")


def test_select_star_on_worker_fails(
    store_dsn: tuple[str, str],
) -> None:
    """SELECT * expands to include credential_hash → permission denied."""
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"SELECT * FROM {schema}.worker LIMIT 1")


def test_select_projected_worker_columns_succeeds(
    store_dsn: tuple[str, str],
) -> None:
    """Explicit projection of allowed columns works."""
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(f"SELECT worker_id, data FROM {schema}.worker LIMIT 1")
        # No row required; the SELECT itself must succeed.


# ----------------------------------------------------------------------
# Hardening
# ----------------------------------------------------------------------


def test_legacy_over_grant_is_revoked(store_dsn: tuple[str, str]) -> None:
    """Pre-existing GRANT SELECT ON ALL TABLES + default-privileges
    legacy state MUST be REVOKEd before the column-level GRANT is
    applied; otherwise SELECT credential_hash would still work.
    """
    schema, _ = store_dsn
    # Manually pre-create the role with a wider grant than 12a-1f
    # would install. The provisioner must REVOKE this before its
    # own GRANTs.
    pwd_legacy = secrets.token_hex(16)
    dbname = (
        psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or "postgres"
    )
    with (
        psycopg.connect(DSN, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            sql.SQL("CREATE ROLE eden_readonly WITH LOGIN PASSWORD {}").format(
                sql.Literal(pwd_legacy)
            )
        )
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO eden_readonly").format(
                db=sql.Identifier(dbname)
            )
        )
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {schema} TO eden_readonly").format(
                schema=sql.Identifier(schema)
            )
        )
        cur.execute(
            sql.SQL(
                "GRANT SELECT ON ALL TABLES IN SCHEMA {schema} "
                "TO eden_readonly"
            ).format(schema=sql.Identifier(schema))
        )
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                "GRANT SELECT ON TABLES TO eden_readonly"
            ).format(schema=sql.Identifier(schema))
        )

    # Verify the over-grant is in effect: credential_hash IS visible
    # before re-provisioning.
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd_legacy)) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(f"SELECT credential_hash FROM {schema}.worker LIMIT 1")
        # The SELECT must succeed (no exception) — confirms the
        # legacy over-grant.

    # Now re-provision with the new posture.
    pwd_new = secrets.token_hex(16)
    _provision(store_dsn, pwd_new)

    # credential_hash MUST no longer be reachable.
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd_new)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"SELECT credential_hash FROM {schema}.worker LIMIT 1")


def test_legacy_non_select_default_privilege_is_revoked(
    store_dsn: tuple[str, str],
) -> None:
    """Codex round-0: a non-SELECT legacy default-privilege grant
    (e.g. ``ALTER DEFAULT PRIVILEGES GRANT INSERT ON TABLES``) MUST
    be REVOKEd by ensure_readonly_role's hardening pass too —
    otherwise a future credential-bearing table could inherit write
    access. Verifies the round-0 expansion of the REVOKE sweep.
    """
    schema, _ = store_dsn
    pwd_legacy = secrets.token_hex(16)
    with (
        psycopg.connect(DSN, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            sql.SQL("CREATE ROLE eden_readonly WITH LOGIN PASSWORD {}").format(
                sql.Literal(pwd_legacy)
            )
        )
        # Pre-install a NON-SELECT default-privilege grant — the
        # original 12a-1f impl only revoked SELECT, so this would
        # have slipped through pre-fix.
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                "GRANT INSERT ON TABLES TO eden_readonly"
            ).format(schema=sql.Identifier(schema))
        )

    pwd_new = secrets.token_hex(16)
    _provision(store_dsn, pwd_new)

    # As eden superuser, create a fresh table. Without the round-0
    # broader-revoke, the readonly role would inherit INSERT.
    with (
        psycopg.connect(DSN, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            sql.SQL("CREATE TABLE {schema}.test_legacy_drift (id int)").format(
                schema=sql.Identifier(schema)
            )
        )

    # eden_readonly MUST NOT have INSERT on the fresh table.
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd_new)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(
            f"INSERT INTO {schema}.test_legacy_drift (id) VALUES (1)"
        )


def test_default_privileges_not_installed(store_dsn: tuple[str, str]) -> None:
    """A freshly-created table in the schema MUST NOT be readable.

    Verifies the deliberate absence of ALTER DEFAULT PRIVILEGES —
    if it were installed, the readonly role would auto-read any
    future table, including one with credential-bearing columns.
    """
    pwd = secrets.token_hex(16)
    _provision(store_dsn, pwd)
    schema, _ = store_dsn
    # Create a fresh table as the superuser.
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE {schema}.test_new (id int)").format(
                schema=sql.Identifier(schema)
            )
        )
        cur.execute(
            sql.SQL("INSERT INTO {schema}.test_new (id) VALUES (1)").format(
                schema=sql.Identifier(schema)
            )
        )
    # Readonly role MUST NOT have SELECT on it.
    with (
        psycopg.connect(_readonly_dsn(store_dsn, pwd)) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cur.execute(f"SELECT * FROM {schema}.test_new")
