"""Live PostgreSQL restore fencing and stale-object replacement."""

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from healthmes.backup.snapshot import (
    _pg_dump_to,
    _pg_restore_from,
    _preflight_pg_target,
    find_pg_tool,
)


def _psycopg_url(database_url: str) -> str:
    return make_url(database_url).set(
        drivername="postgresql"
    ).render_as_string(hide_password=False)


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_restore_fences_sessions_and_removes_stale_objects(
    tmp_path: Path,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    if any(find_pg_tool(name) is None for name in ("pg_dump", "pg_restore", "psql")):
        pytest.skip("PostgreSQL client tools are unavailable")

    token = uuid.uuid4().hex
    snapshot_schema = f"hm_snapshot_{token}"
    stale_schema = f"hm_stale_{token}"
    dump_path = tmp_path / "healthmes.dump"
    conninfo = _psycopg_url(database_url)

    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(snapshot_schema))
        )
        connection.execute(
            sql.SQL("CREATE TABLE {}.probe(value text NOT NULL)").format(
                sql.Identifier(snapshot_schema)
            )
        )
        connection.execute(
            sql.SQL("INSERT INTO {}.probe VALUES ('snapshot')").format(
                sql.Identifier(snapshot_schema)
            )
        )

    blocker = None
    try:
        _pg_dump_to(database_url, dump_path)
        with psycopg.connect(conninfo, autocommit=True) as connection:
            connection.execute(
                sql.SQL("UPDATE {}.probe SET value = 'mutated'").format(
                    sql.Identifier(snapshot_schema)
                )
            )
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(stale_schema))
            )
            connection.execute(
                sql.SQL("CREATE TABLE {}.stale_table(id integer)").format(
                    sql.Identifier(stale_schema)
                )
            )
            connection.execute(
                sql.SQL(
                    "CREATE FUNCTION {}.stale_function() RETURNS integer "
                    "LANGUAGE SQL AS 'SELECT 1'"
                ).format(sql.Identifier(stale_schema))
            )

        blocker = psycopg.connect(conninfo, autocommit=True)
        blocker_pid = blocker.execute("SELECT pg_backend_pid()").fetchone()[0]
        _pg_restore_from(
            database_url,
            dump_path,
            _preflight_pg_target(database_url),
        )

        with psycopg.connect(conninfo, autocommit=True) as connection:
            restored = connection.execute(
                sql.SQL("SELECT value FROM {}.probe").format(
                    sql.Identifier(snapshot_schema)
                )
            ).fetchone()[0]
            stale_namespace = connection.execute(
                "SELECT to_regnamespace(%s)",
                (stale_schema,),
            ).fetchone()[0]
            allow_connections = connection.execute(
                "SELECT datallowconn FROM pg_database WHERE datname = current_database()"
            ).fetchone()[0]
        assert restored == "snapshot"
        assert stale_namespace is None
        assert allow_connections is True

        with pytest.raises(psycopg.Error):
            blocker.execute("SELECT 1")
        assert blocker_pid > 0
    finally:
        if blocker is not None:
            blocker.close()
        with psycopg.connect(conninfo, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(snapshot_schema)
                )
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(stale_schema)
                )
            )
