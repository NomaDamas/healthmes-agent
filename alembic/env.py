"""Alembic environment for the healthmes database.

Modeled on ``vendor/open-wearables/backend/migrations/env.py``, with two
deviations required by this repo's dual run target (mac-native sqlite and
dockerized postgres):

- The URL comes from ``-x db_url=...`` / a programmatically set
  ``sqlalchemy.url`` / ``Settings.database_url`` — passed straight to
  SQLAlchemy (no configparser interpolation issues with ``%``).
- Online mode reuses :func:`healthmes.store.session.create_db_engine` so
  sqlite runs get the same safety settings as the app (foreign keys pragma,
  parent-directory creation for the zero-setup path).
"""

from logging.config import fileConfig

from sqlalchemy import event, pool

from alembic import context
from healthmes.config import get_settings
from healthmes.store import models  # noqa: F401  (register all tables on Base.metadata)
from healthmes.store.base import Base
from healthmes.store.session import create_db_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the migration URL: -x db_url > ini sqlalchemy.url > Settings."""
    x_arguments = context.get_x_argument(as_dictionary=True)
    if x_arguments.get("db_url"):
        return x_arguments["db_url"]
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode: render SQL without a DBAPI connection."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode: connect and execute."""
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _run_online_migrations(supplied_connection)
        return

    url = _database_url()
    connectable = create_db_engine(
        url,
        poolclass=pool.NullPool,
        enforce_runtime_write_fence=False,
    )

    if connectable.dialect.name == "sqlite":
        # The store engine turns PRAGMA foreign_keys=ON at connect time —
        # right for the app, WRONG for migrations: sqlite batch move-and-copy
        # drops the old table, which fires child ON DELETE actions and nulled
        # every task.goal_id during the weekly_goal rebuild (review
        # 2026-07-27). Migrations recreate identical rows immediately, so FK
        # enforcement is disabled for the migration connection only. The
        # listener runs after the store's ON listener (registration order),
        # and at DBAPI connect time — outside any transaction, where the
        # pragma actually takes effect.
        @event.listens_for(connectable, "connect")
        def _migrations_disable_sqlite_fks(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

    try:
        with connectable.connect() as connection:
            _run_online_migrations(connection)
    finally:
        connectable.dispose()


def _run_online_migrations(connection) -> None:
    sqlite_dbapi = None
    restore_sqlite_foreign_keys = False
    if connection.dialect.name == "sqlite":
        sqlite_dbapi = connection.connection.driver_connection
        cursor = sqlite_dbapi.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys")
            foreign_keys_enabled = bool(cursor.fetchone()[0])
        finally:
            cursor.close()
        if foreign_keys_enabled:
            if connection.in_transaction() or sqlite_dbapi.in_transaction:
                raise RuntimeError(
                    "SQLite migrations require PRAGMA foreign_keys=OFF "
                    "before the supplied transaction begins"
                )
            cursor = sqlite_dbapi.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("PRAGMA foreign_keys")
                if cursor.fetchone()[0] != 0:
                    raise RuntimeError(
                        "could not disable SQLite foreign keys for migration"
                    )
            finally:
                cursor.close()
            restore_sqlite_foreign_keys = True

    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Future ALTERs on sqlite need batch mode (harmless elsewhere).
            render_as_batch=connection.dialect.name == "sqlite",
            # PostgreSQL DDL is transactional, so one Alembic command must roll
            # back every revision if a later revision fails. Pysqlite otherwise
            # lets DDL run before a physical BEGIN; migrations that reserve the
            # writer with a no-op UPDATE rely on the same Alembic boundary.
            transactional_ddl=connection.dialect.name
            in {"postgresql", "sqlite"},
        )

        with context.begin_transaction():
            context.run_migrations()
            if sqlite_dbapi is not None:
                violations = connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchmany(1)
                if violations:
                    raise RuntimeError(
                        "SQLite migration produced foreign-key violations"
                    )
    finally:
        if restore_sqlite_foreign_keys:
            assert sqlite_dbapi is not None
            if connection.in_transaction() or sqlite_dbapi.in_transaction:
                connection.rollback()
            cursor = sqlite_dbapi.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA foreign_keys")
                if cursor.fetchone()[0] != 1:
                    raise RuntimeError(
                        "could not restore SQLite foreign-key enforcement "
                        "after migration"
                    )
            finally:
                cursor.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
