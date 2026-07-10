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

from sqlalchemy import pool

from alembic import context
from healthmes.config import get_settings
from healthmes.store import models  # noqa: F401  (register all tables on Base.metadata)
from healthmes.store.base import Base
from healthmes.store.session import create_db_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
    url = _database_url()
    connectable = create_db_engine(url, poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # Future ALTERs on sqlite need batch mode (harmless elsewhere).
                render_as_batch=connection.dialect.name == "sqlite",
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
